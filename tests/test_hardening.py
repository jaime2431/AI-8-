"""Regression tests for the issues found in the independent code review."""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backbone.db import clean_invoices, staging_invoices
from backbone.notify import ConsoleNotifier
from backbone.pipeline import extract_sources, process_documents, promote, run_nightly
from backbone.powerbi import PowerBIClient
from backbone.tenancy import ClientRegistry, _parse_client
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func

from conftest import FIXTURES, Block, FakeClaude, FakePowerBI, drop_csv, drop_ecf, make_ctx
from portal.app import create_app
from test_pipeline import CSV, DAY, fake_reader


def _app(registry):
    built = {}

    def factory(reg, cid):
        built.setdefault(cid, make_ctx(reg, cid, powerbi=FakePowerBI()))
        return built[cid]
    return TestClient(create_app(registry, context_factory=factory, env="dev")), factory


def test_upload_cannot_escape_the_inbox(registry):
    http, factory = _app(registry)
    target = registry.tmp / "beta" / "ecf" / "evil.xml"
    r = http.post("/documents", headers={"X-Dev-Client": "alpha"},
                  files={"file": (str(target), (FIXTURES / "ecf_sale.xml").read_bytes(), "text/xml")})
    assert r.status_code == 200
    assert not target.exists()                          # nothing written into beta's folder


def test_only_reviewers_approve(registry):
    http, factory = _app(registry)
    alpha = factory(registry, "alpha")
    drop_csv(registry, "alpha", "pos.csv", CSV)
    run_nightly(alpha, DAY, ConsoleNotifier(), reader=fake_reader)
    item = http.get("/review-queue", headers={"X-Dev-Client": "alpha"}).json()[0]["id"]
    viewer = {"X-Dev-Client": "alpha", "X-Dev-Roles": "Viewer"}
    assert http.post(f"/review-queue/{item}/approve", json={}, headers=viewer).status_code == 403
    bad_date = http.post(f"/review-queue/{item}/approve", json={"corrections": {"issue_date": "02/10/2026"}},
                         headers={"X-Dev-Client": "alpha"})
    assert bad_date.status_code == 422


def test_uploaded_ecf_is_parsed_without_claude(registry):
    http, factory = _app(registry)
    alpha = factory(registry, "alpha")
    http.post("/documents", headers={"X-Dev-Client": "alpha"},
              files={"file": ("factura.xml", (FIXTURES / "ecf_sale.xml").read_bytes(), "text/xml")})

    def must_not_call(*a, **k):
        raise AssertionError("Claude should not read e-CF XML")
    assert process_documents(alpha, reader=must_not_call)["documents_read"] == 1


def test_sales_document_direction_from_rnc(registry):
    ctx = make_ctx(registry, "alpha")
    pdf = registry.tmp / "venta.pdf"
    pdf.write_bytes(b"%PDF sale")
    from backbone.pipeline import ingest_document
    ingest_document(ctx, pdf)

    def reader(claude, model, data, mt, direction="purchase"):
        row = fake_reader(claude, model, data, mt)
        row["issuer_rnc"] = "131-00000-2"           # the client's own RNC: it is a sale
        return row
    process_documents(ctx, reader)
    with ctx.engine.connect() as conn:
        assert conn.execute(select(staging_invoices.c.direction)).scalar() == "sale"


def test_ncf_is_normalized_so_reruns_detect_duplicates(registry):
    ctx = make_ctx(registry, "alpha")
    drop_csv(registry, "alpha", "a.csv", "Fecha,NCF,RNC,Subtotal,ITBIS,Total\n02/10/2026,b02-0000-0001 ,131000002,500,90,590\n")
    extract_sources(ctx); promote(ctx, DAY + timedelta(days=1))
    drop_csv(registry, "alpha", "b.csv", "Fecha,NCF,RNC,Subtotal,ITBIS,Total\n02/10/2026,B0200000001,131000002,500,90,590\n")
    extract_sources(ctx)
    counts = promote(ctx, DAY + timedelta(days=1))
    assert counts["rejected"] == 1 and counts["error"] == 0
    with ctx.engine.connect() as conn:
        assert conn.execute(select(clean_invoices.c.ncf)).scalars().all() == ["B0200000001"]


def test_broken_ecf_file_does_not_stop_the_run(registry):
    ctx = make_ctx(registry, "alpha")
    drop_ecf(registry, "alpha", "ecf_sale.xml")
    bad = (FIXTURES / "ecf_sale.xml").read_text().replace("11800.00", "11,800.00").replace("E310000000101", "E310000000102")
    (registry.tmp / "alpha" / "ecf" / "bad.xml").write_text(bad)
    stats = extract_sources(ctx)
    assert stats["ecf"] == 1
    assert (registry.tmp / "alpha" / "ecf" / "failed" / "bad.xml").exists()
    assert (registry.tmp / "alpha" / "ecf" / "processed" / "ecf_sale.xml").exists()


def test_files_stay_put_if_saving_fails(registry, monkeypatch):
    ctx = make_ctx(registry, "alpha")
    drop_ecf(registry, "alpha", "ecf_sale.xml")
    import backbone.pipeline as pl

    def boom(*a, **k):
        raise RuntimeError("database down")
    monkeypatch.setattr(pl, "stage_rows", boom)
    stats = extract_sources(ctx)
    assert stats["ecf"] == {"error": "database down"}                        # reported, other sources continue
    assert (registry.tmp / "alpha" / "ecf" / "ecf_sale.xml").exists()      # not moved, will be retried


def test_refresh_wait_ignores_older_refreshes():
    started = datetime(2026, 10, 3, 4, 30)
    answers = iter([
        {"status": "Completed", "startTime": "2026-10-02T04:30:00Z"},        # yesterday's: ignore
        {"status": "Unknown", "startTime": "2026-10-03T04:30:05Z"},
        {"status": "Completed", "startTime": "2026-10-03T04:30:05Z"},
    ])
    pbi = PowerBIClient("w", "d", token_provider=lambda: "t")
    pbi.latest_refresh = lambda: next(answers)
    assert pbi.wait_for_refresh(poll_s=0, started_after=started)["startTime"].startswith("2026-10-03")


def test_client_ids_must_be_safe_and_distinct():
    base = {"name": "X", "own_rnc": "131000002", "entra_tenant_id": "T1"}
    with pytest.raises(ValueError):
        _parse_client({"client_id": "Ferreteria Norte", **base})
    a = _parse_client({"client_id": "ferreteria-norte", **base})
    assert a.entra_tenant_id == "t1"                    # tenant ids compared in lower case
    reg = ClientRegistry({"ferreteria-norte": a})
    assert reg.by_entra_tenant("T1").client_id == "ferreteria-norte"


def test_ecf_folder_skips_acknowledgments(registry):
    ctx = make_ctx(registry, "alpha")
    drop_ecf(registry, "alpha", "ecf_usd_signed.xml", "arecf.xml")
    assert extract_sources(ctx)["ecf"] == 1
    assert (registry.tmp / "alpha" / "ecf" / "skipped" / "arecf.xml").exists()


def test_portal_screens_are_served_and_feed_from_the_api(registry):
    http, factory = _app(registry)
    page = http.get("/")
    assert page.status_code == 200 and "<!doctype html>" in page.text and "Facturas por revisar" in page.text
    assert http.get("/auth-config").json() == {"mode": "dev"}
    cfg = http.get("/config", headers={"X-Dev-Client": "alpha", "X-Dev-Roles": "Viewer"}).json()
    assert cfg["company"] == "Empresa alpha" and cfg["roles"] == ["Viewer"] and cfg["report_embed_url"] is None
    alpha = factory(registry, "alpha")
    pdf = registry.tmp / "f.pdf"; pdf.write_bytes(b"%PDF x")
    from backbone.pipeline import ingest_document
    ingest_document(alpha, pdf, "whatsapp")
    docs = http.get("/documents", headers={"X-Dev-Client": "alpha"}).json()
    assert docs[0]["filename"] == "f.pdf" and docs[0]["status"] == "pending"
    assert http.get("/documents", headers={"X-Dev-Client": "beta"}).json() == []


def test_portal_security_headers_and_review_fields(registry):
    http, factory = _app(registry)
    page = http.get("/")
    csp = page.headers["content-security-policy"]
    nonce = csp.split("'nonce-")[1].split("'")[0]
    assert f'<script nonce="{nonce}">' in page.text and "object-src 'none'" in csp
    alpha = factory(registry, "alpha")
    drop_csv(registry, "alpha", "pos.csv", CSV)
    run_nightly(alpha, DAY, ConsoleNotifier(), reader=fake_reader)
    item = http.get("/review-queue", headers={"X-Dev-Client": "alpha"}).json()[0]
    assert item["subtotal"] is not None and item["itbis"] is not None and item["created_at"].endswith("Z")
    assert http.get("/runs/latest", headers={"X-Dev-Client": "alpha"}).json()["started_at"].endswith("Z")


def test_one_bad_row_does_not_stop_the_batch(registry, monkeypatch):
    """promote(): a row that fails while loading is marked 'error'; the others still load."""
    import backbone.pipeline as pl
    from backbone.pipeline import promote, stage_rows
    ctx = make_ctx(registry, "alpha")
    rows = [{"direction": "sale", "issuer_rnc": "131000002", "ncf": f"B020000000{i}", "issue_date": DAY, "currency": "DOP",
             "subtotal": Decimal("100"), "itbis": Decimal("18"), "total": Decimal("118")} for i in (1, 2, 3)]
    with ctx.engine.begin() as conn:
        stage_rows(conn, rows, "pos")
    real = pl._load_clean

    def flaky(conn, srow, row, vctx, approved_by):
        if row["ncf"].endswith("2"):
            raise RuntimeError("disk full")
        return real(conn, srow, row, vctx, approved_by)
    monkeypatch.setattr(pl, "_load_clean", flaky)
    counts = promote(ctx, DAY + timedelta(days=1))
    assert counts == {"accepted": 2, "review": 0, "rejected": 0, "error": 1}
    with ctx.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(clean_invoices)).scalar() == 2
        assert conn.execute(select(staging_invoices.c.status).where(staging_invoices.c.ncf == "B0200000002")).scalar() == "error"


def test_over_long_values_do_not_abort_staging(registry):
    from backbone.pipeline import stage_rows
    ctx = make_ctx(registry, "alpha")
    with ctx.engine.begin() as conn:
        n = stage_rows(conn, [{"direction": "sale", "issuer_rnc": "1310000021234", "ncf": "B0200000001", "currency": "PESOS",
                               "issuer_name": "x" * 400, "issue_date": DAY, "subtotal": 1, "itbis": 0, "total": 1}], "pos")
    assert n == 1
    with ctx.engine.connect() as conn:
        r = conn.execute(select(staging_invoices.c.issuer_rnc, staging_invoices.c.currency, staging_invoices.c.issuer_name)).one()
    assert r.issuer_rnc is None and r.currency is None and len(r.issuer_name) == 255


def test_rls_user_reaches_power_bi(registry):
    from backbone.agents.assistant import answer
    pbi = FakePowerBI({"VENTAS": 1})
    seen = []
    orig = pbi.execute_dax
    pbi.execute_dax = lambda q, max_rows=1000, impersonated_user=None: (seen.append(impersonated_user), orig(q))[1]
    claude = FakeClaude([[Block("tool_use", name="run_dax", input={"query": "EVALUATE ROW(\"v\", [VENTAS])"})],
                         [Block("text", text="ok")]])
    answer(claude, "m", pbi, "Alpha", "", "¿ventas?", impersonated_user="ana@alpha.do")
    assert seen == ["ana@alpha.do"]


def test_second_run_is_refused_while_one_is_running(registry):
    from sqlalchemy import insert
    from backbone.db import pipeline_runs
    ctx = make_ctx(registry, "alpha")
    with ctx.engine.begin() as conn:
        conn.execute(insert(pipeline_runs).values(run_date=DAY, status="running"))
    with pytest.raises(RuntimeError, match="already in progress"):
        run_nightly(ctx, DAY, ConsoleNotifier(), reader=fake_reader)


def test_webhook_payload_matches_platform():
    from backbone.notify import webhook_payload
    assert webhook_payload("https://hooks.slack.com/services/x", "hi") == {"text": "hi"}
    teams = webhook_payload("https://prod-12.westus.logic.azure.com:443/workflows/x", "hi")
    assert teams["attachments"][0]["content"]["body"][0]["text"] == "hi"


def test_connection_string_has_no_integrated_security():
    from sqlalchemy.engine import make_url
    from sqlalchemy.dialects.mssql.pyodbc import MSDialect_pyodbc
    from infra.onboard import sql_url
    url = make_url(sql_url("sql-x.database.windows.net", "clean", "abc-123"))
    args, _ = MSDialect_pyodbc().create_connect_args(url)
    conn_str = args[0]
    assert "Authentication=ActiveDirectoryMsi" in conn_str and "UID=abc-123" in conn_str
    assert "Trusted_Connection" not in conn_str and "PWD" not in conn_str
