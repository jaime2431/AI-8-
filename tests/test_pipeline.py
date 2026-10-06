from datetime import date
from decimal import Decimal

from sqlalchemy import func, select

from backbone.db import clean_invoices, pipeline_runs, review_queue, staging_invoices
from backbone.notify import ConsoleNotifier
from backbone.pipeline import approve_review_item, ingest_document, run_nightly
from conftest import FakePowerBI, drop_csv, drop_ecf, make_ctx

DAY = date(2026, 10, 2)

CSV = (
    "Fecha,NCF,RNC,Subtotal,ITBIS,Total\n"
    "02/10/2026,B0200000001,131000002,500.00,90.00,590.00\n"      # good POS sale
    "02/10/2026,B0200000002,131000002,500.00,90.00,999.00\n"      # totals do not add up -> review
)


def fake_reader(claude, model, data, media_type, direction="purchase"):
    return {"direction": "purchase", "issuer_rnc": "132567897", "issuer_name": "Taller Gomez", "ncf": "B0100000777",
            "buyer_rnc": "131000002",
            "issue_date": "2026-10-02", "currency": "DOP", "subtotal": Decimal("2000"), "itbis": Decimal("360"),
            "total": Decimal("2360"), "confidence": {"total": 0.97, "ncf": 0.62}}   # unsure about the NCF


def setup_alpha(registry, pbi):
    ctx = make_ctx(registry, "alpha", powerbi=pbi)
    drop_ecf(registry, "alpha", "ecf_sale.xml", "ecf_purchase.xml")
    drop_csv(registry, "alpha", "pos_2026-10-02.csv", CSV)
    pdf = registry.tmp / "factura.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake invoice")
    assert ingest_document(ctx, pdf, "whatsapp") is not None
    assert ingest_document(ctx, pdf, "whatsapp") is None          # same file twice is ignored
    return ctx


def test_nightly_run_passes_when_power_bi_matches(registry):
    # clean sales for the day: e-CF 11,800 + POS 590 ; purchases: e-CF 5,860 (document waits in review)
    pbi = FakePowerBI({"VENTAS": 12390.00, "COMPRAS": 5860.00})
    ctx = setup_alpha(registry, pbi)
    notifier = ConsoleNotifier()
    result = run_nightly(ctx, DAY, notifier, reader=fake_reader)

    assert result["status"] == "passed", result
    assert result["stats"]["accepted"] == 3 and result["stats"]["review"] == 2
    assert pbi.refreshes == 1
    assert "DATE(2026, 10, 2)" in pbi.queries[0]
    assert notifier.sent[0][0] == "summary"
    with ctx.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(clean_invoices)).scalar() == 3
        assert conn.execute(select(func.count()).select_from(review_queue)).scalar() == 2
        assert conn.execute(select(pipeline_runs.c.status)).scalar() == "passed"


def test_nightly_run_is_held_when_power_bi_disagrees(registry):
    pbi = FakePowerBI({"VENTAS": 9000.00, "COMPRAS": 5860.00})
    ctx = setup_alpha(registry, pbi)
    notifier = ConsoleNotifier()
    result = run_nightly(ctx, DAY, notifier, reader=fake_reader)
    assert result["status"] == "held"
    assert notifier.sent[0][0] == "alert" and "ventas" in notifier.sent[0][1]


def test_failed_refresh_holds_the_summary(registry):
    ctx = setup_alpha(registry, FakePowerBI(refresh_status="Failed"))
    notifier = ConsoleNotifier()
    assert run_nightly(ctx, DAY, notifier, reader=fake_reader)["status"] == "held"


def test_rerun_does_not_duplicate(registry):
    pbi = FakePowerBI({"VENTAS": 12390.00, "COMPRAS": 5860.00})
    ctx = setup_alpha(registry, pbi)
    run_nightly(ctx, DAY, ConsoleNotifier(), reader=fake_reader)
    drop_ecf(registry, "alpha", "ecf_sale.xml")        # the provider sends the same e-CF again
    second = run_nightly(ctx, DAY, ConsoleNotifier(), reader=fake_reader)
    assert second["stats"]["rejected"] == 1
    with ctx.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(clean_invoices)).scalar() == 3


def test_human_approval_with_correction(registry):
    ctx = setup_alpha(registry, FakePowerBI({"VENTAS": 12390.00, "COMPRAS": 5860.00}))
    run_nightly(ctx, DAY, ConsoleNotifier(), reader=fake_reader)
    with ctx.engine.connect() as conn:
        items = conn.execute(select(review_queue.c.id, staging_invoices.c.source)
                             .join(staging_invoices, staging_invoices.c.id == review_queue.c.staging_id)).all()
    doc_item = next(i.id for i in items if i.source == "document")
    pos_item = next(i.id for i in items if i.source == "pos")

    # low confidence on the NCF: a person confirms it -> loads
    assert approve_review_item(ctx, doc_item, "ana", today=date(2026, 10, 3))["approved"] is True
    # bad totals cannot be approved as-is ...
    r = approve_review_item(ctx, pos_item, "ana", today=date(2026, 10, 3))
    assert r["approved"] is False and "totals_add_up" in r["problems"][0]
    # ... but can be approved after correcting the total
    assert approve_review_item(ctx, pos_item, "ana", {"total": "590.00"}, today=date(2026, 10, 3))["approved"]
    with ctx.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(clean_invoices)).scalar() == 5


def test_credit_notes_are_negative(registry):
    from backbone.pipeline import stage_rows, promote
    ctx = make_ctx(registry, "alpha")
    with ctx.engine.begin() as conn:
        stage_rows(conn, [{"direction": "sale", "issuer_rnc": "131000002", "ncf": "E340000000001",
                           "ncf_modified": "E310000000101", "issue_date": DAY, "currency": "DOP",
                           "subtotal": Decimal("1000"), "itbis": Decimal("180"), "total": Decimal("1180")}], "ecf")
    assert promote(ctx, DAY)["accepted"] == 1
    with ctx.engine.connect() as conn:
        row = conn.execute(select(clean_invoices.c.total_dop, clean_invoices.c.ncf_modified, clean_invoices.c.ncf_type)).one()
    assert row.total_dop == Decimal("-1180.00") and row.ncf_modified == "E310000000101" and row.ncf_type == "E34"


def test_transient_document_failure_is_retried_once(registry):
    from backbone.db import documents
    from backbone.pipeline import ingest_document, process_documents
    ctx = make_ctx(registry, "alpha")
    pdf = registry.tmp / "retry.pdf"; pdf.write_bytes(b"%PDF retry")
    ingest_document(ctx, pdf)
    calls = []

    def flaky(claude, model, data, mt, direction="purchase"):
        calls.append(1)
        if len(calls) < 2:
            raise ConnectionError("Claude unreachable")
        return fake_reader(claude, model, data, mt)
    assert process_documents(ctx, flaky)["documents_failed"] == 1
    with ctx.engine.connect() as conn:
        assert conn.execute(select(documents.c.status)).scalar() == "retry"
    assert process_documents(ctx, flaky)["documents_read"] == 1          # next night it goes through
    # a file that is not an invoice is never retried
    bad = registry.tmp / "bad.xml"; bad.write_bytes(b"<ARECF/>")
    ingest_document(ctx, bad)
    process_documents(ctx, flaky)
    with ctx.engine.connect() as conn:
        assert conn.execute(select(documents.c.status).where(documents.c.filename == "bad.xml")).scalar() == "failed"


def test_reprocessing_an_old_day_uses_the_real_calendar(registry):
    from backbone.pipeline import stage_rows
    ctx = make_ctx(registry, "alpha")
    with ctx.engine.begin() as conn:
        stage_rows(conn, [{"direction": "sale", "issuer_rnc": "131000002", "ncf": "B0200000777", "issue_date": date(2026, 10, 4),
                           "currency": "DOP", "subtotal": Decimal("100"), "itbis": Decimal("18"), "total": Decimal("118")}], "pos")
    result = run_nightly(ctx, date(2026, 9, 1), ConsoleNotifier(), reader=fake_reader, today=date(2026, 10, 5))
    assert result["stats"]["accepted"] == 1          # an Oct 4 invoice is not "in the future" on Oct 5
