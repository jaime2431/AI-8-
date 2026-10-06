from datetime import date
from decimal import Decimal

from backbone.ecf import parse_ecf
from backbone.validation import ValidationContext, ncf_type, rnc_checksum_ok, validate_invoice
from conftest import FIXTURES, OWN_RNC

TODAY = date(2026, 10, 4)


def good_row(**over):
    row = dict(direction="purchase", issuer_rnc="401506254", ncf="B0100000123", issue_date=date(2026, 10, 2),
               currency="DOP", subtotal=Decimal("1000.00"), itbis=Decimal("180.00"), total=Decimal("1180.00"),
               confidence={"issuer_rnc": 0.99, "ncf": 0.98, "total": 0.99})
    row.update(over)
    return row


def ctx(**over):
    return ValidationContext(today=TODAY, open_period_start=date(2026, 1, 1), **over)


def test_rnc_check_digit():
    assert rnc_checksum_ok("401506254")          # DGII's own RNC
    assert not rnc_checksum_ok("401506253")
    assert rnc_checksum_ok(OWN_RNC)


def test_ncf_formats():
    assert ncf_type("B0100000123") == "B01"
    assert ncf_type("E310000000101") == "E31"
    assert ncf_type("B9900000123") is None       # unknown type
    assert ncf_type("B01000001") is None          # too short


def test_good_row_is_accepted():
    d = validate_invoice(good_row(), ctx())
    assert d.status == "accepted", d.reason()


def test_totals_that_do_not_add_up_go_to_review():
    d = validate_invoice(good_row(total=Decimal("1250.00")), ctx())
    assert d.status == "review"
    assert "totals_add_up" in d.reason()


def test_duplicate_is_rejected():
    d = validate_invoice(good_row(), ctx(existing_keys={("401506254", "B0100000123")}))
    assert d.status == "rejected"


def test_low_confidence_goes_to_review():
    d = validate_invoice(good_row(confidence={"total": 0.6}), ctx())
    assert d.status == "review" and "confidence" in d.reason()


def test_usd_needs_banco_central_rate():
    row = good_row(currency="USD")
    assert validate_invoice(row, ctx()).status == "review"
    assert validate_invoice(row, ctx(fx_rates={date(2026, 10, 2): Decimal("63.50")})).status == "accepted"


def test_future_and_closed_period_dates():
    assert validate_invoice(good_row(issue_date=date(2026, 10, 9)), ctx()).status == "review"
    assert validate_invoice(good_row(issue_date=date(2025, 12, 31)), ctx()).status == "review"


def test_itbis_rate_checked_against_taxable_amount():
    assert validate_invoice(good_row(taxable_amount=Decimal("1000")), ctx()).status == "accepted"
    bad = good_row(taxable_amount=Decimal("1000"), itbis=Decimal("150"), total=Decimal("1150"))
    assert "itbis_rate" in validate_invoice(bad, ctx()).reason()


def test_unknown_supplier_only_when_required():
    assert validate_invoice(good_row(), ctx(known_suppliers=None)).status == "accepted"
    assert validate_invoice(good_row(), ctx(known_suppliers=set())).status == "review"


def test_ecf_sale_and_purchase_parse_and_validate():
    sale = parse_ecf((FIXTURES / "ecf_sale.xml").read_bytes(), OWN_RNC)
    assert sale["direction"] == "sale" and sale["ncf"] == "E310000000101"
    assert sale["total"] == Decimal("11800.00") and sale["issue_date"] == date(2026, 10, 2)
    purchase = parse_ecf((FIXTURES / "ecf_purchase.xml").read_bytes(), OWN_RNC)   # has an XML namespace
    assert purchase["direction"] == "purchase" and purchase["expected_itbis"] == Decimal("860.00")
    for row in (sale, purchase):
        d = validate_invoice(row, ctx())
        assert d.status == "accepted", d.reason()


def test_signed_usd_ecf_keeps_pesos_and_records_dollars():
    row = parse_ecf((FIXTURES / "ecf_usd_signed.xml").read_bytes(), OWN_RNC)
    assert row["direction"] == "purchase" and row["currency"] == "DOP"
    assert row["total"] == Decimal("71614.44")                  # pesos, as DGII requires in Totales
    assert row["foreign_currency"] == "USD" and row["foreign_total"] == Decimal("1180.00")
    assert row["lines"][0]["description"] == "Bomba de agua 1HP"
    d = validate_invoice(row, ctx())
    assert d.status == "accepted", d.reason()


def test_acknowledgment_files_are_not_invoices():
    import pytest
    from backbone.ecf import NotAnInvoice
    with pytest.raises(NotAnInvoice):
        parse_ecf((FIXTURES / "arecf.xml").read_bytes(), OWN_RNC)
