"""Regression tests for the fourth review (Oct 6): forged uploads, direction, credit-note signs,
missing-key duplicates, ITBIS sign and date corrections."""
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backbone.db import clean_invoices, review_queue, staging_invoices
from backbone.ecf import parse_ecf
from backbone.pipeline import approve_review_item, process_documents, promote, stage_rows
from backbone.validation import ValidationContext, validate_invoice
from conftest import OWN_RNC, FakePowerBI, make_ctx
from portal.app import create_app

TODAY = date(2026, 10, 5)

UNSIGNED_SALE = b"""<?xml version="1.0"?><ECF><Encabezado><IdDoc><eNCF>E310000099999</eNCF></IdDoc>
<Emisor><RNCEmisor>131000002</RNCEmisor><RazonSocialEmisor>Own</RazonSocialEmisor><FechaEmision>02-10-2026</FechaEmision></Emisor>
<Comprador><RNCComprador>401506254</RNCComprador></Comprador>
<Totales><MontoGravadoTotal>1000000.00</MontoGravadoTotal><TotalITBIS>180000.00</TotalITBIS><MontoTotal>1180000.00</MontoTotal></Totales>
</Encabezado></ECF>"""


def _http(registry):
    built = {}

    def factory(reg, cid):
        built.setdefault(cid, make_ctx(reg, cid, powerbi=FakePowerBI()))
        return built[cid]
    return TestClient(create_app(registry, context_factory=factory, env="dev")), factory


def _review_item(ctx, **over):
    row = dict(direction="sale", issuer_rnc=OWN_RNC, ncf="B0400000007", issue_date=date(2026, 10, 1),
               currency="DOP", subtotal="1000", itbis="180", total="1180", confidence={"total": 0.5})
    row.update(over)
    with ctx.engine.begin() as c:
        stage_rows(c, [row], "document")
    promote(ctx, TODAY)
    with ctx.engine.connect() as c:
        return c.execute(select(review_queue.c.id)).scalar()


def test_xml_uploaded_in_portal_waits_for_a_person(registry):
    http, factory = _http(registry)
    alpha = factory(registry, "alpha")
    r = http.post("/documents", headers={"X-Dev-Client": "alpha", "X-Dev-Roles": "Viewer"},
                  files={"file": ("x.xml", UNSIGNED_SALE, "text/xml")})
    assert r.status_code == 200
    process_documents(alpha, reader=None)
    assert promote(alpha, TODAY)["review"] == 1
    with alpha.engine.connect() as c:
        assert c.execute(select(clean_invoices)).first() is None
        assert "unverified_upload" in c.execute(select(review_queue.c.reason)).scalar()


def test_someone_elses_invoice_is_not_our_purchase():
    row = parse_ecf(UNSIGNED_SALE.replace(b"131000002", b"101010632"), OWN_RNC)
    assert row["direction"] == "purchase"
    d = validate_invoice(row, ValidationContext(today=TODAY, own_rnc=OWN_RNC))
    assert d.status == "review" and "buyer_is_us" in d.reason()


def test_credito_fiscal_purchase_without_buyer_goes_to_review():
    row = dict(direction="purchase", issuer_rnc="101010632", ncf="B0100000123", issue_date=TODAY,
               currency="DOP", subtotal="1000", itbis="180", total="1180")
    assert validate_invoice(row, ValidationContext(today=TODAY, own_rnc=OWN_RNC)).status == "review"
    row["buyer_rnc"] = OWN_RNC
    assert validate_invoice(row, ValidationContext(today=TODAY, own_rnc=OWN_RNC)).status == "accepted"


@pytest.mark.parametrize("encf", ["E410000000001", "E430000000001", "E470000000001"])
def test_self_issued_purchase_types_are_expenses(encf):
    xml = f"""<ECF><Encabezado><IdDoc><eNCF>{encf}</eNCF></IdDoc>
<Emisor><RNCEmisor>131000002</RNCEmisor><FechaEmision>02-10-2026</FechaEmision></Emisor>
<Comprador><RNCComprador>00113918205</RNCComprador></Comprador>
<Totales><MontoExento>5000.00</MontoExento><MontoTotal>5000.00</MontoTotal></Totales></Encabezado></ECF>""".encode()
    row = parse_ecf(xml, OWN_RNC)
    assert row["direction"] == "purchase"
    assert validate_invoice(row, ValidationContext(today=TODAY, own_rnc=OWN_RNC)).status == "accepted"


def test_corrected_amounts_keep_a_credit_note_negative(registry):
    ctx = make_ctx(registry, "alpha")
    item = _review_item(ctx)
    res = approve_review_item(ctx, item, "u1", {"subtotal": "1000", "itbis": "180", "total": "1180"}, today=TODAY)
    assert res["approved"]
    with ctx.engine.connect() as c:
        r = c.execute(select(clean_invoices.c.total, clean_invoices.c.total_dop)).first()
    assert r.total == Decimal("-1180.00") and r.total_dop == Decimal("-1180.00")


def test_correcting_the_ncf_to_a_credit_note_makes_it_negative(registry):
    ctx = make_ctx(registry, "alpha")
    item = _review_item(ctx, ncf="B0100000007")
    assert approve_review_item(ctx, item, "u1", {"ncf": "B0400000007"}, today=TODAY)["approved"]
    with ctx.engine.connect() as c:
        r = c.execute(select(clean_invoices.c.ncf_type, clean_invoices.c.total)).first()
    assert r.ncf_type == "B04" and r.total < 0


def test_rows_missing_ncf_are_not_duplicates_of_each_other(registry):
    ctx = make_ctx(registry, "alpha")
    rows = [dict(direction="purchase", issuer_rnc="401506254", buyer_rnc=OWN_RNC, ncf=None, issue_date=date(2026, 10, 1),
                 currency="DOP", subtotal=s, itbis="0", total=s, source_ref=f"doc{i}")
            for i, s in enumerate(("100", "250", "999"))]
    with ctx.engine.begin() as c:
        stage_rows(c, rows, "document")
    promote(ctx, TODAY)
    with ctx.engine.connect() as c:
        statuses = [s for (s,) in c.execute(select(staging_invoices.c.status).order_by(staging_invoices.c.id))]
    assert statuses == ["review", "review", "review"]


@pytest.mark.parametrize("sub,itbis,total,taxable", [
    ("-1000", "-500", "-1500", None),     # credit note with 50% ITBIS
    ("1000", "-180", "820", None),        # negative ITBIS on an invoice
    ("1000", "0", "1000", "1000"),        # taxable amount but no ITBIS
])
def test_implausible_itbis_goes_to_review(sub, itbis, total, taxable):
    row = dict(direction="sale", issuer_rnc=OWN_RNC, ncf="B0100000001", issue_date=date(2026, 10, 1), currency="DOP",
               subtotal=Decimal(sub), itbis=Decimal(itbis), total=Decimal(total))
    if taxable:
        row["taxable_amount"] = Decimal(taxable)
    d = validate_invoice(row, ValidationContext(today=TODAY))
    assert d.status == "review" and "itbis_rate" in d.reason()


def test_credit_note_with_18_percent_itbis_passes():
    row = dict(direction="sale", issuer_rnc=OWN_RNC, ncf="B0400000001", issue_date=date(2026, 10, 1), currency="DOP",
               subtotal=Decimal("-1000"), itbis=Decimal("-180"), total=Decimal("-1180"))
    assert validate_invoice(row, ValidationContext(today=TODAY)).status == "accepted"


def test_a_date_correction_that_is_not_a_date_is_refused(registry):
    ctx = make_ctx(registry, "alpha")
    item = _review_item(ctx, ncf="B0100000008")
    with pytest.raises(ValueError):
        approve_review_item(ctx, item, "u1", {"issue_date": 20991231}, today=TODAY)


def test_check_ecf_applies_the_buyer_rule(tmp_path, capsys):
    from backbone import cli
    f = tmp_path / "otra.xml"
    f.write_bytes(UNSIGNED_SALE.replace(b"131000002", b"101010632"))
    import sys
    argv, sys.argv = sys.argv, ["cli", "check-ecf", str(f), "--own-rnc", OWN_RNC]
    try:
        cli.main(["check-ecf", str(f), "--own-rnc", OWN_RNC])
    finally:
        sys.argv = argv
    out = capsys.readouterr().out
    assert "REVIEW" in out and "buyer_is_us" in out
