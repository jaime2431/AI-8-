from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import insert, select

from backbone import cli
from backbone.db import audit_log, clean_invoices
from backbone.dgii_formats import build_606, build_607, file_name
from conftest import OWN_RNC, make_ctx

SUPPLIER = "401506254"
CEDULA = "00112345678"
PERIOD = "2026-09"


def sale(**over):
    row = dict(id=1, direction="sale", issuer_rnc=OWN_RNC, buyer_rnc=SUPPLIER, ncf="B0100000001", ncf_modified=None,
               issue_date=date(2026, 9, 5), currency="DOP", fx_rate=None, subtotal=Decimal("1000.00"),
               itbis=Decimal("180.00"), other_taxes=Decimal("0"), tip=Decimal("0"), total=Decimal("1180.00"))
    row.update(over)
    return row


def purchase(**over):
    row = sale(direction="purchase", issuer_rnc=SUPPLIER, buyer_rnc=OWN_RNC, ncf="B0100000777")
    row.update(over)
    return row


def lines(text):
    head, *detail = text.split("\r\n")
    return head, [d.split("|") for d in detail]


def test_607_header_and_fields():
    text, _ = build_607([sale(), sale(id=2, ncf="B0100000002")], OWN_RNC, PERIOD)
    head, detail = lines(text)
    assert head == f"607|{OWN_RNC}|202609|2"
    assert all(len(f) == 23 for f in detail)
    f = detail[0]
    assert f[:4] == [SUPPLIER, "1", "B0100000001", ""]
    assert f[5] == "20260905"                       # fecha comprobante AAAAMMDD
    assert (f[7], f[8]) == ("1000.00", "180.00")   # monto facturado sin impuestos, ITBIS facturado
    assert not text.endswith("\r\n")                # like DGII's tool: no break after the last line


def test_606_header_and_fields():
    text, _ = build_606([purchase(tip=Decimal("100"), other_taxes=Decimal("25.5"))], OWN_RNC, "202609")
    head, detail = lines(text)
    assert head == f"606|{OWN_RNC}|202609|1"
    f = detail[0]
    assert len(f) == 23
    assert f[:7] == [SUPPLIER, "1", "", "B0100000777", "", "20260905", ""]
    assert f[7:11] == ["", "", "1000.00", "180.00"]    # servicios/bienes unknown, total monto facturado known
    assert f[14] == "180.00"                            # ITBIS por adelantar = facturado - llevado al costo
    assert (f[20], f[21], f[22]) == ("25.50", "100.00", "")


def test_amount_and_date_formatting():
    text, _ = build_607([sale(subtotal=Decimal("1234567.5"), itbis=Decimal("0"), issue_date=date(2026, 9, 30))],
                        OWN_RNC, PERIOD)
    f = lines(text)[1][0]
    assert (f[5], f[7], f[8]) == ("20260930", "1234567.50", "0.00")


def test_sale_purchase_filtering():
    rows = [sale(), purchase()]
    assert lines(build_607(rows, OWN_RNC, PERIOD)[0])[1][0][2] == "B0100000001"
    assert lines(build_606(rows, OWN_RNC, PERIOD)[0])[1][0][3] == "B0100000777"
    assert build_607(rows, OWN_RNC, PERIOD)[0].startswith(f"607|{OWN_RNC}|202609|1")
    assert build_606(rows, OWN_RNC, PERIOD)[0].startswith(f"606|{OWN_RNC}|202609|1")


def test_rows_of_another_company_are_left_out():
    text, warnings = build_606([purchase(buyer_rnc="101010101")], OWN_RNC, PERIOD)
    assert text == f"606|{OWN_RNC}|202609|0"
    assert any("not 131000002" in w for w in warnings)


def test_period_filtering():
    rows = [sale(issue_date=date(2026, 8, 31)), sale(id=2, ncf="B0100000002"),
            sale(id=3, ncf="B0100000003", issue_date=date(2026, 10, 1))]
    _, detail = lines(build_607(rows, OWN_RNC, PERIOD)[0])
    assert [f[2] for f in detail] == ["B0100000002"]
    december = build_607([sale(issue_date=date(2026, 12, 31))], OWN_RNC, "2026-12")[0]
    assert december.startswith(f"607|{OWN_RNC}|202612|1")


def test_credit_note_positive_and_references_modified():
    note = purchase(ncf="B0400000009", ncf_modified="B0100000777", subtotal=Decimal("-200"), itbis=Decimal("-36"),
                    total=Decimal("-236"))
    text, warnings = build_606([note], OWN_RNC, PERIOD)
    f = lines(text)[1][0]
    assert (f[3], f[4], f[9], f[10]) == ("B0400000009", "B0100000777", "200.00", "36.00")
    assert not any("modified NCF" in w for w in warnings)


def test_credit_note_without_modified_ncf_warns():
    note = sale(ncf="E340000000010", subtotal=Decimal("-200"), itbis=Decimal("-36"))
    _, warnings = build_606([purchase(ncf="E340000000010")], OWN_RNC, PERIOD)
    assert any("E340000000010: credit note without the modified NCF" in w for w in warnings)
    text, warnings = build_607([note.copy() | {"ncf": "B0400000010"}], OWN_RNC, PERIOD)
    assert lines(text)[1][0][3] == ""
    assert any("B0400000010: credit note without the modified NCF" in w for w in warnings)
    assert not any("B0400000010: forma de venta" in w for w in warnings)   # DGII exempts credit notes


def test_usd_amounts_converted_with_stored_rate():
    row = purchase(currency="USD", fx_rate=Decimal("60.5000"), subtotal=Decimal("100.00"), itbis=Decimal("18.00"))
    f = lines(build_606([row], OWN_RNC, PERIOD)[0])[1][0]
    assert (f[9], f[10]) == ("6050.00", "1089.00")


def test_usd_without_rate_is_left_out():
    text, warnings = build_606([purchase(currency="USD", fx_rate=None)], OWN_RNC, PERIOD)
    assert text.endswith("|0")
    assert any("without an exchange rate" in w for w in warnings)


def test_cedula_vs_rnc_id_type():
    rows = [purchase(issuer_rnc=CEDULA), purchase(id=2, ncf="B0100000778")]
    _, detail = lines(build_606(rows, OWN_RNC, PERIOD)[0])
    assert [(f[0], f[1]) for f in detail] == [(CEDULA, "2"), (SUPPLIER, "1")]
    _, detail = lines(build_607([sale(buyer_rnc=CEDULA)], OWN_RNC, PERIOD)[0])
    assert (detail[0][0], detail[0][1]) == (CEDULA, "2")


def test_warnings_for_missing_required_data():
    _, warnings = build_606([purchase()], OWN_RNC, PERIOD)
    assert any("tipo de bienes" in w for w in warnings)
    assert any("forma de pago" in w for w in warnings)
    _, warnings = build_607([sale(buyer_rnc=None)], OWN_RNC, PERIOD)
    assert any("tipo de ingreso" in w for w in warnings)
    assert any("forma de venta" in w for w in warnings)
    assert any("buyer RNC/cédula missing" in w for w in warnings)


def test_operator_codes_fill_required_fields_without_warnings():
    text, warnings = build_606([purchase()], OWN_RNC, PERIOD, tipo_bienes="09", forma_pago="02")
    f = lines(text)[1][0]
    assert (f[2], f[22]) == ("09", "02")
    assert not any("tipo de bienes" in w or "forma de pago" in w for w in warnings)
    f = lines(build_607([sale()], OWN_RNC, PERIOD, tipo_ingreso="1")[0])[1][0]
    assert f[4] == "1"
    with pytest.raises(ValueError):
        build_606([], OWN_RNC, PERIOD, tipo_bienes="12")


def test_607_small_consumer_invoices_and_ecf_go_to_summary():
    rows = [sale(ncf="B0200000001", buyer_rnc=None), sale(id=2, ncf="E310000000001"),
            sale(id=3, ncf="B0200000002", buyer_rnc=CEDULA, subtotal=Decimal("300000"), itbis=Decimal("54000"),
                 total=Decimal("354000"))]
    text, warnings = build_607(rows, OWN_RNC, PERIOD)
    _, detail = lines(text)
    assert [f[2] for f in detail] == ["B0200000002"]
    assert any("1 facturas de consumo under RD$250,000" in w and "monto 1000.00" in w for w in warnings)
    assert any("1 e-CF sales not in the file" in w for w in warnings)


def test_606_accepts_e_ncf():
    f = lines(build_606([purchase(ncf="E310000000005")], OWN_RNC, PERIOD)[0])[1][0]
    assert f[3] == "E310000000005"


def test_cli_writes_file_and_audits(registry, tmp_path, capsys):
    ctx = make_ctx(registry, "alpha")
    base = dict(source="ecf", approved_by="rules", currency="DOP", subtotal=Decimal("1000"), itbis=Decimal("180"),
                total=Decimal("1180"), issue_date=date(2026, 9, 5))
    with ctx.engine.begin() as conn:
        conn.execute(insert(clean_invoices), [
            dict(base, staging_id=1, direction="sale", issuer_rnc=OWN_RNC, buyer_rnc=SUPPLIER, ncf="B0100000001"),
            dict(base, staging_id=2, direction="purchase", issuer_rnc=SUPPLIER, buyer_rnc=OWN_RNC, ncf="B0100000002"),
            dict(base, staging_id=3, direction="sale", issuer_rnc=OWN_RNC, buyer_rnc=SUPPLIER, ncf="B0100000003",
                 issue_date=date(2026, 10, 1)),
        ])
    out = tmp_path / "607.txt"
    cli.main(["--clients-file", str(registry.tmp / "clients.json"), "dgii", "--client", "alpha",
              "--period", PERIOD, "--format", "607", "--out", str(out)])
    data = out.read_bytes().decode()
    assert data.split("\r\n")[0] == f"607|{OWN_RNC}|202609|1"
    assert "B0100000001" in data and "B0100000003" not in data
    printed = capsys.readouterr().out
    assert "WARNING B0100000001: tipo de ingreso" in printed and "1 records" in printed
    with ctx.engine.connect() as conn:
        assert conn.execute(select(audit_log.c.action).where(audit_log.c.actor == "cli")).scalar() == "dgii_file"
    assert file_name("606", OWN_RNC, PERIOD) == f"DGII_F_606_{OWN_RNC}_202609.TXT"
