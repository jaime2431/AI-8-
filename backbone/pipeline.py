"""Nightly pipeline for ONE client (run once per client, never across clients).

  1. extract    connectors -> staging_invoices
  2. read       pending documents -> Claude invoice reader -> staging_invoices
  3. validate   staging 'new' rows -> clean_invoices | review_queue | rejected
  4. refresh    Power BI refresh from the clean tables, wait for it
  5. verify     quality check: Power BI vs clean DB + volume
  6. send       morning summary if all checks pass, otherwise hold it and alert our team
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import shutil
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection

from .agents import quality_check as qa
from .agents.invoice_reader import ExtractionError, read_document
from .connectors import build_connector
from .context import ClientContext
from .db import (audit, business_today, clean_invoices, connector_state, documents, fx_rates, master_suppliers,
                 pipeline_runs, review_queue, staging_invoices, utcnow)
from .ecf import parse_ecf
from .notify import ConsoleNotifier, Notifier
from .validation import (ValidationContext, clean_rnc, invoice_direction, ncf_type, normalize_ncf, to_decimal,
                         validate_invoice)

XML_TYPES = {"application/xml", "text/xml"}

INVOICE_FIELDS = ("direction", "issuer_rnc", "issuer_name", "buyer_rnc", "ncf", "ncf_modified", "issue_date", "currency",
                  "subtotal", "taxable_amount", "itbis", "expected_itbis", "other_taxes", "tip", "total", "branch")
CREDIT_NOTE_TYPES = {"B04", "E34"}     # notas de crédito: stored as negative amounts so totals net out
MONEY_FIELDS = {"subtotal", "taxable_amount", "itbis", "expected_itbis", "other_taxes", "tip", "total"}
HUMAN_OVERRIDABLE = {"confidence", "known_supplier", "rnc_registry_missing", "unverified_upload"}   # a person can confirm these; never duplicates or totals
# Column widths of staging_invoices: values are checked here so one bad value never aborts a batch on SQL Server
TEXT_LIMITS = {"issuer_rnc": 11, "buyer_rnc": 11, "ncf": 13, "ncf_modified": 13, "currency": 3, "direction": 10,
               "issuer_name": 255, "branch": 100}
MAX_DOCUMENT_BYTES = 15 * 1024 * 1024
MAX_DOCUMENTS_PER_RUN = 500          # a cost ceiling per client per night; the rest wait for the next night
RUN_LOCK_HOURS = 6


def _json(v) -> str:
    return json.dumps(v, default=str, ensure_ascii=False)


# ---------- intake ----------

def ingest_document(ctx: ClientContext, src: str | Path, channel: str = "upload") -> int | None:
    """Copies a file into the client's inbox and registers it. Returns None if it was already received."""
    src = Path(src)
    data = src.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    inbox = Path(ctx.config.inbox_path or f"./data/{ctx.client_id}/inbox")
    inbox.mkdir(parents=True, exist_ok=True)
    with ctx.engine.begin() as conn:
        if conn.execute(select(documents.c.id).where(documents.c.sha256 == sha)).first():
            return None
        dest = inbox / f"{sha[:16]}{src.suffix.lower()}"
        shutil.copyfile(src, dest)
        media = mimetypes.guess_type(src.name)[0] or "application/octet-stream"
        doc_id = conn.execute(insert(documents).values(
            channel=channel, filename=src.name, storage_path=str(dest), media_type=media, sha256=sha)).inserted_primary_key[0]
        audit(conn, "system", "document_received", {"id": doc_id, "file": src.name, "channel": channel})
        return doc_id


def stage_rows(conn: Connection, rows, source: str, document_id: int | None = None) -> int:
    n = 0
    for row in rows:
        if "_error" in row:
            audit(conn, "system", "extract_error", {"source": source, "error": row["_error"]})
            continue
        values = {f: row.get(f) for f in INVOICE_FIELDS}
        values["issuer_rnc"] = clean_rnc(values["issuer_rnc"])
        values["buyer_rnc"] = clean_rnc(values["buyer_rnc"])
        values["ncf"] = normalize_ncf(values["ncf"])
        values["ncf_modified"] = normalize_ncf(values.get("ncf_modified"))
        for f, limit in TEXT_LIMITS.items():                 # too long: keep names, drop impossible codes
            v = values.get(f)
            if isinstance(v, str) and len(v) > limit:
                values[f] = v[:limit] if f in ("issuer_name", "branch") else None
        _sign_credit_note(values)
        if isinstance(values["issue_date"], str):
            try:
                values["issue_date"] = date.fromisoformat(values["issue_date"])
            except ValueError:
                values["issue_date"] = None
        try:
            with conn.begin_nested():
                conn.execute(insert(staging_invoices).values(
                    source=source, source_ref=str(row.get("source_ref") or "")[:500], document_id=document_id,
                    lines_json=_json(row.get("lines") or []), confidence_json=_json(row.get("confidence") or {}),
                    status="new", **values))
            n += 1
        except Exception as e:                               # one unsaveable row never loses the batch
            audit(conn, "system", "stage_error", {"source": source, "ref": row.get("source_ref"), "error": str(e)[:500]})
    return n


def _sign_credit_note(values: dict) -> None:
    """Credit notes are stored negative so totals net out, whatever sign the source (or a reviewer) used."""
    if values.get("ncf") and ncf_type(values["ncf"]) in CREDIT_NOTE_TYPES:
        for f in MONEY_FIELDS:
            v = to_decimal(values.get(f))
            if v is not None and v > 0:
                values[f] = -v


def extract_sources(ctx: ClientContext) -> dict:
    """Rows are saved first; only after the commit are files moved to processed/ and the
    connector's watermark advanced, so a failed save never loses data."""
    stats: dict = {}
    for spec in ctx.config.connectors:
        name = spec.get("name", spec.get("type", "?"))
        try:
            source = build_connector(spec, ctx)
            started = utcnow()
            with ctx.engine.connect() as conn:
                state = conn.execute(select(connector_state.c.last_run).where(connector_state.c.name == source.name)).first()
            since = state.last_run if state else None
            rows = list(source.extract(since=since))
            clock = getattr(source, "source_clock", lambda: None)()     # ERP sources: watermark on the ERP's clock
            with ctx.engine.begin() as conn:
                stats[source.name] = stage_rows(conn, rows, source.name)
                mark = clock or started
                if state:
                    conn.execute(update(connector_state).where(connector_state.c.name == source.name).values(last_run=mark))
                else:
                    conn.execute(insert(connector_state).values(name=source.name, last_run=mark))
            commit = getattr(source, "commit", None)
            if commit:
                commit()
        except Exception as e:                   # one unreachable system must not stop the others
            stats[name] = {"error": str(e)[:300]}
            with ctx.engine.begin() as conn:
                audit(conn, "system", "connector_failed", {"source": name, "error": str(e)[:500]})
    return stats


def process_documents(ctx: ClientContext, reader: Callable = read_document, retry_failed: bool = True) -> dict:
    """Reads pending documents. Documents that failed on a previous night get one more try (a Claude or
    network outage should not lose them); a second failure leaves them failed for a person to look at."""
    ok = failed = 0
    with ctx.engine.begin() as conn:
        statuses = ["pending", "retry"] if retry_failed else ["pending"]
        pending = conn.execute(select(documents).where(documents.c.status.in_(statuses))
                               .order_by(documents.c.id).limit(MAX_DOCUMENTS_PER_RUN)).mappings().all()
    for doc in pending:
        try:
            data = Path(doc["storage_path"]).read_bytes()
            if len(data) > MAX_DOCUMENT_BYTES:
                raise ExtractionError("file larger than 15 MB; ask the client for a smaller scan")
            if doc["media_type"] in XML_TYPES:
                row = parse_ecf(data, ctx.config.own_rnc)        # an uploaded e-CF needs no AI
                # (an uploaded acknowledgment raises NotAnInvoice and is marked failed with that reason)
                # XML sent by hand through the portal is unsigned as far as we know: a person confirms it
                source = "portal_xml" if doc["channel"] == "upload" else "document"
            else:
                row = reader(ctx.claude, ctx.model_agent, data, doc["media_type"])
                row["direction"] = invoice_direction(row.get("issuer_rnc"), row.get("ncf"), ctx.config.own_rnc)
                source = "document"
            row["source_ref"] = doc["filename"]
            with ctx.engine.begin() as conn:
                stage_rows(conn, [row], source, doc["id"])
                conn.execute(update(documents).where(documents.c.id == doc["id"]).values(status="extracted"))
            ok += 1
        except Exception as e:
            new_status = "retry" if doc["status"] == "pending" and not _is_permanent(e) else "failed"
            with ctx.engine.begin() as conn:
                conn.execute(update(documents).where(documents.c.id == doc["id"]).values(status=new_status, error=str(e)[:2000]))
                audit(conn, "system", "document_failed", {"id": doc["id"], "error": str(e), "status": new_status})
            failed += 1
    return {"documents_read": ok, "documents_failed": failed}


def _is_permanent(e: Exception) -> bool:
    """Errors a retry cannot fix: the file itself is not an invoice, cannot be read, or is rejected by the model."""
    from .ecf import ECFError
    if isinstance(e, (ECFError, ExtractionError)):
        return True
    try:
        import anthropic
        return isinstance(e, anthropic.BadRequestError)
    except ImportError:
        return False


# ---------- validation ----------

def _registry_for(ctx: ClientContext, rncs) -> dict | None:
    """DGII statuses for the RNCs in this batch, only if the registry was loaded in the last 3 days."""
    from .reference import lookup_rncs, registry_is_fresh
    try:
        eng = ctx.reference_engine
        if eng is None or not registry_is_fresh(eng):
            return None
        return lookup_rncs(eng, rncs)
    except Exception as e:             # reference database down: validate without the registry, never fail the run
        print(f"[{ctx.client_id}] DGII registry unavailable: {e}")
        return None


def build_validation_context(conn: Connection, ctx: ClientContext, today: date, rncs=()) -> ValidationContext:
    keys = {(r.issuer_rnc, r.ncf) for r in conn.execute(select(clean_invoices.c.issuer_rnc, clean_invoices.c.ncf))}
    rates = {r.rate_date: Decimal(str(r.usd_dop)) for r in conn.execute(select(fx_rates))}
    known = None
    if ctx.config.require_known_suppliers:
        known = {r.rnc for r in conn.execute(select(master_suppliers.c.rnc))}
    return ValidationContext(today=today, open_period_start=ctx.config.open_period_start,
                             confidence_threshold=ctx.config.confidence_threshold,
                             existing_keys=keys, fx_rates=rates, known_suppliers=known, own_rnc=ctx.config.own_rnc,
                             rnc_registry=_registry_for(ctx, rncs) if rncs else None)


def _row_for_validation(srow) -> dict:
    row = {f: srow[f] for f in INVOICE_FIELDS}
    row["confidence"] = json.loads(srow["confidence_json"] or "{}")
    row["source"] = srow["source"]
    return row


def _load_clean(conn: Connection, srow, row: dict, vctx: ValidationContext, approved_by: str) -> None:
    rate = None
    total = to_decimal(row["total"])
    if (row.get("currency") or "").upper() == "USD":
        rate = vctx.fx_rates[row["issue_date"]]
    values = {f: row.get(f) for f in INVOICE_FIELDS}
    conn.execute(insert(clean_invoices).values(
        staging_id=srow["id"], ncf_type=ncf_type(row["ncf"]), fx_rate=rate,
        total_dop=(total * rate).quantize(Decimal("0.01")) if rate else total,
        source=srow["source"], source_ref=srow["source_ref"], document_id=srow["document_id"],
        approved_by=approved_by, **values))
    vctx.existing_keys.add((row["issuer_rnc"], row["ncf"]))
    if row.get("direction") == "purchase" and row.get("issuer_rnc") and row["issuer_rnc"] != clean_rnc(vctx.own_rnc):
        if not conn.execute(select(master_suppliers.c.rnc).where(master_suppliers.c.rnc == row["issuer_rnc"])).first():
            conn.execute(insert(master_suppliers).values(rnc=row["issuer_rnc"], name=row.get("issuer_name")))


def _open_review_keys(conn: Connection) -> set[tuple[str, str]]:
    q = (select(staging_invoices.c.issuer_rnc, staging_invoices.c.ncf)
         .join(review_queue, review_queue.c.staging_id == staging_invoices.c.id)
         .where(review_queue.c.status == "open"))
    return {(r.issuer_rnc, r.ncf) for r in conn.execute(q) if r.issuer_rnc and r.ncf}


def promote(ctx: ClientContext, today: date) -> dict:
    """Each row is validated inside its own savepoint: one bad row is marked 'error'
    and the rest of the batch still loads."""
    counts = {"accepted": 0, "review": 0, "rejected": 0, "error": 0}
    with ctx.engine.begin() as conn:
        new_rows = conn.execute(select(staging_invoices).where(staging_invoices.c.status == "new")
                                .order_by(staging_invoices.c.id)).mappings().all()
        rncs = {r["issuer_rnc"] for r in new_rows} | {r["buyer_rnc"] for r in new_rows}
        vctx = build_validation_context(conn, ctx, today, rncs)
        waiting = _open_review_keys(conn)
        for srow in new_rows:
            row = _row_for_validation(srow)
            key = (row.get("issuer_rnc"), row.get("ncf"))
            keyed = bool(key[0] and key[1])       # rows missing RNC or NCF are not duplicates of each other
            already_known = key in vctx.existing_keys
            try:
                with conn.begin_nested():
                    if keyed and key in waiting:
                        status, reason = "rejected", "same invoice already waiting in the review queue"
                        rules = [{"rule": "no_duplicate", "outcome": "reject", "message": reason}]
                    else:
                        decision = validate_invoice(row, vctx)
                        status, reason = decision.status, decision.reason()
                        rules = [r.__dict__ for r in decision.results]
                    conn.execute(update(staging_invoices).where(staging_invoices.c.id == srow["id"]).values(
                        status=status, rules_json=_json(rules)))
                    if status == "accepted":
                        _load_clean(conn, srow, row, vctx, "rules")
                    elif status == "review":
                        conn.execute(insert(review_queue).values(staging_id=srow["id"], reason=reason))
                        if keyed:
                            waiting.add(key)
                    else:
                        audit(conn, "rules", "row_rejected", {"staging_id": srow["id"], "reason": reason})
                counts[status] += 1
            except Exception as e:
                if not already_known:
                    vctx.existing_keys.discard(key)
                conn.execute(update(staging_invoices).where(staging_invoices.c.id == srow["id"]).values(
                    status="error", rules_json=_json([{"rule": "load", "outcome": "error", "message": str(e)[:500]}])))
                audit(conn, "system", "row_error", {"staging_id": srow["id"], "error": str(e)[:500]})
                counts["error"] += 1
    return counts


def approve_review_item(ctx: ClientContext, item_id: int, user: str, corrections: dict | None = None,
                        today: date | None = None) -> dict:
    """A person approves (optionally correcting fields). Confidence and new-supplier flags are
    overridden by the human; every other rule must still pass."""
    today = today or business_today()
    with ctx.engine.begin() as conn:
        item = conn.execute(select(review_queue).where(review_queue.c.id == item_id)).mappings().first()
        if item is None or item["status"] != "open":
            raise LookupError("review item not found or already resolved")
        srow = conn.execute(select(staging_invoices).where(staging_invoices.c.id == item["staging_id"])).mappings().first()
        row = _row_for_validation(srow)
        for k, v in (corrections or {}).items():
            if k not in INVOICE_FIELDS:
                continue
            if k == "issue_date":
                if isinstance(v, str):
                    v = date.fromisoformat(v)      # ValueError -> the portal answers 422
                elif not isinstance(v, date):
                    raise ValueError("issue_date must be a date like 2026-10-03")
            elif k in MONEY_FIELDS:
                v = to_decimal(v)
            elif k in ("issuer_rnc", "buyer_rnc"):
                v = clean_rnc(v)
            elif k == "ncf":
                v = normalize_ncf(v)
            row[k] = v
        _sign_credit_note(row)                     # a corrected amount or NCF must not flip a credit note positive
        vctx = build_validation_context(conn, ctx, today, {row.get("issuer_rnc"), row.get("buyer_rnc")})
        decision = validate_invoice(row, vctx)
        blocking = [r for r in decision.problems() if r.rule not in HUMAN_OVERRIDABLE]
        if blocking:
            return {"approved": False, "problems": [f"{r.rule}: {r.message}" for r in blocking]}
        _load_clean(conn, srow, row, vctx, f"user:{user}")
        conn.execute(update(staging_invoices).where(staging_invoices.c.id == srow["id"]).values(status="accepted"))
        conn.execute(update(review_queue).where(review_queue.c.id == item_id).values(
            status="approved", resolved_by=user, resolved_at=utcnow(), corrections_json=_json(corrections or {})))
        audit(conn, f"user:{user}", "review_approved", {"item": item_id, "corrections": corrections or {}})
        return {"approved": True, "problems": []}


def reject_review_item(ctx: ClientContext, item_id: int, user: str, note: str = "") -> None:
    with ctx.engine.begin() as conn:
        item = conn.execute(select(review_queue).where(review_queue.c.id == item_id)).mappings().first()
        if item is None or item["status"] != "open":
            raise LookupError("review item not found or already resolved")
        conn.execute(update(review_queue).where(review_queue.c.id == item_id).values(
            status="rejected", resolved_by=user, resolved_at=utcnow(), corrections_json=_json({"note": note})))
        conn.execute(update(staging_invoices).where(staging_invoices.c.id == item["staging_id"]).values(status="rejected"))
        audit(conn, f"user:{user}", "review_rejected", {"item": item_id, "note": note})


# ---------- the nightly run ----------

def run_nightly(ctx: ClientContext, day: date | None = None, notifier: Notifier | None = None,
                reader: Callable = read_document, refresh_timeout_s: int = 3600, today: date | None = None) -> dict:
    """day = the business day being closed (default yesterday); today = the real calendar day used by the
    date rules, so reprocessing an old day never flags newer invoices as 'in the future'."""
    now_local = business_today()
    day = day or (now_local - timedelta(days=1))
    today = today or max(now_local, day + timedelta(days=1))
    notifier = notifier or ConsoleNotifier()
    with ctx.engine.begin() as conn:
        # Run lock: a second run while one is in progress would send documents to Claude twice.
        stale = utcnow() - timedelta(hours=RUN_LOCK_HOURS)
        conn.execute(update(pipeline_runs).where(pipeline_runs.c.status == "running", pipeline_runs.c.started_at < stale)
                     .values(status="failed", finished_at=utcnow(), stats_json=_json({"error": "abandoned (no finish)"})))
        active = conn.execute(select(pipeline_runs.c.id).where(pipeline_runs.c.status == "running")).first()
        if active:
            raise RuntimeError(f"a run (#{active.id}) is already in progress for {ctx.client_id}")
        run_id = conn.execute(insert(pipeline_runs).values(run_date=day)).inserted_primary_key[0]
    started = utcnow()

    stats: dict = {}
    checks: list[qa.CheckResult] = []
    status = "passed"
    try:
        stats["extracted"] = extract_sources(ctx)
        for name, v in stats["extracted"].items():
            if isinstance(v, dict) and "error" in v:
                checks.append(qa.CheckResult(f"source_{name}", False, note=v["error"]))
        stats.update(process_documents(ctx, reader))
        stats.update(promote(ctx, today=today))

        if ctx.powerbi is not None:
            ctx.powerbi.trigger_refresh()
            refresh = ctx.powerbi.wait_for_refresh(timeout_s=refresh_timeout_s, started_after=started)
            ok = refresh.get("status") == "Completed"
            checks.append(qa.CheckResult("powerbi_refresh", ok, note="" if ok else f"refresh {refresh.get('status')}"))
            if ok:
                with ctx.engine.connect() as conn:
                    checks += qa.compare_with_powerbi(conn, ctx.powerbi, ctx.config.qa_checks, day)
        with ctx.engine.connect() as conn:
            checks.append(qa.volume_check(conn, day))
        if not all(c.ok for c in checks):
            status = "held"
    except Exception as e:
        status = "failed"
        stats["error"] = str(e)

    summary = None
    try:
        if status == "passed":
            try:
                claude = ctx.claude
            except Exception:
                claude = None
            with ctx.engine.connect() as conn:
                summary = qa.write_summary(claude, ctx.model_fast, ctx.config.name, day, checks, stats, conn)
            try:
                notifier.send_summary(ctx.client_id, ctx.config.summary_recipients, summary)
            except Exception as e:               # numbers are fine but the owner did not get them: hold
                status, stats["notify_error"] = "held", str(e)
                notifier.alert(ctx.client_id, f"Run {day}: summary could not be delivered ({e})")
        else:
            failed = [f"{c.name} ({c.note})" for c in checks if not c.ok]
            notifier.alert(ctx.client_id, f"Run {day} {status}. " + (stats.get("error") or "; ".join(failed)))
    except Exception as e:
        stats["notify_error"] = str(e)
    finally:
        with ctx.engine.begin() as conn:
            conn.execute(update(pipeline_runs).where(pipeline_runs.c.id == run_id).values(
                finished_at=utcnow(), status=status, stats_json=_json(stats),
                qa_json=_json([c.to_dict() for c in checks]), summary=summary))
            audit(conn, "pipeline", "nightly_run", {"day": day, "status": status})
    return {"run_id": run_id, "status": status, "stats": stats, "checks": [c.to_dict() for c in checks]}
