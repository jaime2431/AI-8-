"""Command line for operators and the scheduler.

  python -m backbone.cli init-db   --client ferreteria-norte
  python -m backbone.cli ingest    --client ferreteria-norte --file factura.pdf
  python -m backbone.cli nightly   --client ferreteria-norte [--date 2026-10-03]
  python -m backbone.cli nightly-all            # loops clients one by one, each in its own context
  python -m backbone.cli review    --client ferreteria-norte
  python -m backbone.cli fx        --client ferreteria-norte --date 2026-10-03 --rate 63.25
  python -m backbone.cli dgii      --client ferreteria-norte --period 2026-09 --format 607 [--out file.TXT]
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from decimal import Decimal

from sqlalchemy import insert, select

from . import settings
from .context import open_client
from . import dgii_formats
from .db import audit, business_today, clean_invoices, create_tables, fx_rates, review_queue, staging_invoices
from .notify import ConsoleNotifier, EmailNotifier, WebhookNotifier
from .pipeline import ingest_document, run_nightly
from . import reference
from .tenancy import ClientRegistry


def notifier_for(ctx):
    """Owner summaries go by email (smtp-url secret); our team's alerts go to the client's channel (webhook secret).
    Anything not configured falls back to the job log, never to silence."""
    console = ConsoleNotifier()
    email = None
    if ctx.config.smtp_url_secret:
        try:
            email = EmailNotifier(ctx.secrets.get(ctx.config.smtp_url_secret), fallback=console)
        except Exception as e:
            print(f"[{ctx.client_id}] email unavailable, summaries go to the log: {e}")
    if ctx.config.alert_webhook_secret:
        try:
            return WebhookNotifier(ctx.secrets.get(ctx.config.alert_webhook_secret), fallback=console, summary_sender=email)
        except Exception as e:
            print(f"[{ctx.client_id}] webhook unavailable, alerts go to the log: {e}")
    return email or console


def main(argv=None):
    p = argparse.ArgumentParser(prog="backbone")
    p.add_argument("--clients-file", default=settings.CLIENTS_FILE)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("init-db", "ingest", "nightly", "review", "fx"):
        s = sub.add_parser(name)
        s.add_argument("--client", required=True)
        if name == "ingest":
            s.add_argument("--file", required=True)
            s.add_argument("--channel", default="upload")
        if name in ("nightly", "fx"):
            s.add_argument("--date", type=date.fromisoformat)
        if name == "fx":
            s.add_argument("--rate", type=Decimal, required=True)
    sub.add_parser("nightly-all")
    s = sub.add_parser("dgii", help="write the DGII 606 (compras) or 607 (ventas) TXT for one month")
    s.add_argument("--client", required=True)
    s.add_argument("--period", required=True, help="YYYY-MM")
    s.add_argument("--format", required=True, choices=("606", "607"))
    s.add_argument("--out", help="default: DGII_F_<format>_<RNC>_<AAAAMM>.TXT in the current folder")
    s.add_argument("--tipo-bienes", help="606: tipo de bienes y servicios code (01-11) for every row")
    s.add_argument("--forma-pago", help="606: forma de pago code (01-07) for every row")
    s.add_argument("--tipo-ingreso", help="607: tipo de ingreso code (1-6) for every row")
    s = sub.add_parser("check-ecf", help="read real e-CF files and show what the system would load (no database)")
    s.add_argument("files", nargs="+")
    s.add_argument("--own-rnc", required=True, help="RNC of the company the files belong to")
    s = sub.add_parser("refresh-rnc", help="download the DGII RNC registry into the reference database")
    s.add_argument("--file", help="use a local DGII_RNC.zip instead of downloading")
    s = sub.add_parser("refresh-fx", help="load the Banco Central USD rate into every client database")
    s.add_argument("--file", help="use a local copy of the Banco Central notice (PDF) instead of downloading")
    s.add_argument("--rate-type", choices=("sell", "buy"), help="override every client's own fx_rate_type")
    a = p.parse_args(argv)

    if a.cmd == "check-ecf":
        from .ecf import NotAnInvoice, parse_ecf
        from .validation import ValidationContext, validate_invoice
        for f in a.files:
            try:
                row = parse_ecf(open(f, "rb").read(), a.own_rnc)
            except NotAnInvoice as e:
                print(f"{f}: skipped, not an invoice ({e})"); continue
            except Exception as e:
                print(f"{f}: ERROR {e}"); continue
            d = validate_invoice(row, ValidationContext(today=business_today(), own_rnc=a.own_rnc))
            keys = ("direction", "ncf", "issuer_rnc", "buyer_rnc", "issue_date", "subtotal", "itbis", "total",
                    "foreign_currency", "foreign_total")
            print(f"{f}: {d.status.upper()}  " + "  ".join(f"{k}={row.get(k)}" for k in keys if row.get(k) is not None))
            for r in d.problems():
                print(f"    - {r.rule}: {r.message}")
        return

    if a.cmd == "refresh-rnc":
        if not settings.REFERENCE_DB_URL:
            raise SystemExit("set DATIA_REFERENCE_DB_URL first")
        data = open(a.file, "rb").read() if a.file else reference.download(reference.DGII_RNC_URL)
        n = reference.load_rnc_registry(reference.make_engine(settings.REFERENCE_DB_URL), reference.read_rnc_zip(data))
        print(f"RNC registry loaded: {n} rows")
        return

    registry = ClientRegistry.from_file(a.clients_file)

    if a.cmd == "refresh-fx":
        data = open(a.file, "rb").read() if a.file else reference.download(reference.BCRD_RATE_URL)
        notice = reference.parse_bcrd_notice(reference.pdf_text(data))
        print(f"Banco Central: buy {notice['buy']} sell {notice['sell']} valid {notice['from']} to {notice['to']}")
        failures = 0
        for cid in registry.ids():
            try:
                ctx = open_client(registry, cid)
                with ctx.engine.begin() as conn:
                    reference.save_rate(conn, notice, a.rate_type or ctx.config.fx_rate_type)
            except Exception as e:
                failures += 1
                print(f"[{cid}] rate not saved: {e}")
        raise SystemExit(1 if failures else 0)

    if a.cmd == "nightly-all":
        failures = 0
        for cid in registry.ids():
            try:
                ctx = open_client(registry, cid)      # fresh context per client: no shared connections or keys
                r = run_nightly(ctx, notifier=notifier_for(ctx))
                print(json.dumps(r, default=str, ensure_ascii=False))
                failures += r["status"] != "passed"   # held or failed nights make the job exit non-zero
            except Exception as e:                    # one client's problem never stops the others
                failures += 1
                print(json.dumps({"client": cid, "status": "failed", "error": str(e)}))
        raise SystemExit(1 if failures else 0)

    ctx = open_client(registry, a.client)
    if a.cmd == "init-db":
        create_tables(ctx.engine)
        print(f"tables ready for {a.client}")
    elif a.cmd == "ingest":
        print({"document_id": ingest_document(ctx, a.file, a.channel)})
    elif a.cmd == "nightly":
        r = run_nightly(ctx, a.date, notifier_for(ctx))
        print(json.dumps(r, default=str, ensure_ascii=False, indent=2))
        raise SystemExit(0 if r["status"] == "passed" else 1)
    elif a.cmd == "review":
        with ctx.engine.connect() as conn:
            q = select(review_queue.c.id, review_queue.c.reason, staging_invoices.c.ncf, staging_invoices.c.total) \
                .join(staging_invoices, staging_invoices.c.id == review_queue.c.staging_id) \
                .where(review_queue.c.status == "open")
            for r in conn.execute(q):
                print(f"#{r.id}  {r.ncf}  {r.total}  {r.reason}")
    elif a.cmd == "fx":
        with ctx.engine.begin() as conn:
            reference.save_manual_rate(conn, a.date, a.rate)
        print("rate saved (manual; the daily Banco Central load will not overwrite it)")
    elif a.cmd == "dgii":
        write_dgii(ctx, a)


def write_dgii(ctx, a):
    """Builds the 606/607 from clean_invoices for the month, prints every warning and writes the file."""
    start, end = dgii_formats.period_bounds(a.period)
    with ctx.engine.begin() as conn:
        rows = conn.execute(select(clean_invoices).where(clean_invoices.c.issue_date >= start,
                                                         clean_invoices.c.issue_date < end)
                            .order_by(clean_invoices.c.issue_date, clean_invoices.c.id)).mappings().all()
        if a.format == "606":
            text, warnings = dgii_formats.build_606(rows, ctx.config.own_rnc, a.period, a.tipo_bienes, a.forma_pago)
        else:
            text, warnings = dgii_formats.build_607(rows, ctx.config.own_rnc, a.period, a.tipo_ingreso)
        out = a.out or dgii_formats.file_name(a.format, ctx.config.own_rnc, a.period)
        with open(out, "w", encoding="utf-8", newline="") as f:     # newline="": keep DGII's CRLF as built
            f.write(text)
        records = text.count("\r\n")
        audit(conn, "cli", "dgii_file", {"format": a.format, "period": a.period, "file": str(out),
                                         "records": records, "warnings": len(warnings)})
    for w in warnings:
        print(f"WARNING {w}")
    print(f"{a.format} written to {out}: {records} records, {len(warnings)} warnings")


if __name__ == "__main__":
    main()
