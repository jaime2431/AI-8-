"""Daily public reference data: DGII RNC registry and Banco Central USD rate."""
import io
import zipfile
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from backbone.db import fx_rates, make_engine
from backbone.notify import ConsoleNotifier
from backbone.pipeline import run_nightly
from backbone.reference import (ReferenceFormatError, load_rnc_registry, lookup_rncs, parse_bcrd_notice,
                                parse_rnc_lines, pdf_text, read_rnc_zip, registry_is_fresh, save_rate)
from conftest import FakePowerBI, drop_csv, drop_ecf, make_ctx
from test_pipeline import DAY, fake_reader


def dgii_line(rnc, name, status, regime="NORMAL"):
    return f"{rnc}|{name}|{name} COMERCIAL|COMERCIO AL POR MAYOR|||||01/01/2010|{status}|{regime}"


def make_zip(lines):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("TMP/DGII_RNC.TXT", ("\r\n".join(lines) + "\r\n").encode("latin-1"))
    return buf.getvalue()


def test_parse_rnc_zip_latin1_and_bad_rows():
    lines = [dgii_line("401506254", "DIRECCIÓN GENERAL DE IMPUESTOS INTERNOS", "ACTIVO"),
             dgii_line("101005009", "COMPAÑÍA  CARIBE   SRL", "SUSPENDIDO"),
             "line|with|too|few|columns",
             dgii_line("12", "BAD RNC", "ACTIVO")]
    rows = list(read_rnc_zip(make_zip(lines)))
    assert [r["rnc"] for r in rows] == ["401506254", "101005009"]
    assert rows[0]["name"] == "DIRECCIÓN GENERAL DE IMPUESTOS INTERNOS"
    assert rows[1]["name"] == "COMPAÑÍA CARIBE SRL" and rows[1]["status"] == "SUSPENDIDO"


def test_changed_layout_is_refused():
    lines = [f"{100000000 + i}|NAME|X|Y|||||01/01/2010|SOMETHING ELSE|R" for i in range(50)]
    with pytest.raises(ReferenceFormatError):
        list(parse_rnc_lines(lines))


def test_small_file_never_replaces_registry(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 'ref.db'}")
    with pytest.raises(ReferenceFormatError):
        load_rnc_registry(eng, [{"rnc": "401506254", "name": "X", "trade_name": "", "status": "ACTIVO", "regime": ""}])


def test_registry_load_and_lookup(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 'ref.db'}")
    rows = list(parse_rnc_lines([dgii_line("401506254", "DGII", "ACTIVO"), dgii_line("101005009", "CARIBE", "SUSPENDIDO")]))
    assert load_rnc_registry(eng, rows, min_rows=1) == 2
    assert registry_is_fresh(eng)
    assert lookup_rncs(eng, ["401506254", "101005009", "999999999"]) == {"401506254": "ACTIVO", "101005009": "SUSPENDIDO"}


def bcrd_pdf() -> bytes:
    """A PDF laid out like the Banco Central daily notice."""
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    y = 800
    for line in ["BANCO CENTRAL DE LA REPUBLICA DOMINICANA", "DEPARTAMENTO DE TESORERIA",
                 "INFORMACION TASA DE CAMBIO", "Santo Domingo, 2 de octubre de 2026",
                 "Tasa de compra: RD$60.1698/US$", "Tasa de venta: RD$60.6902/US$",
                 "Estas tasas serviran de referencia para las operaciones de esta institucion",
                 "hasta el dia 5 de octubre de 2026."]:
        c.drawString(60, y, line)
        y -= 20
    c.save()
    return buf.getvalue()


def test_bcrd_notice_parsed_from_pdf():
    notice = parse_bcrd_notice(pdf_text(bcrd_pdf()), today=date(2026, 10, 3))
    assert notice == {"buy": Decimal("60.1698"), "sell": Decimal("60.6902"),
                      "from": date(2026, 10, 2), "to": date(2026, 10, 5)}


def test_bcrd_notice_rejects_nonsense():
    with pytest.raises(ReferenceFormatError):
        parse_bcrd_notice("Tasa RD$6.01 RD$6.06 2 de octubre de 2026", today=date(2026, 10, 3))


def test_rate_saved_for_every_day_it_covers(registry):
    ctx = make_ctx(registry, "alpha")
    notice = parse_bcrd_notice(pdf_text(bcrd_pdf()), today=date(2026, 10, 3))
    with ctx.engine.begin() as conn:
        assert save_rate(conn, notice) == 4                     # Fri to Mon
        save_rate(conn, notice, "buy")                          # re-run updates, no duplicates
    with ctx.engine.connect() as conn:
        rates = dict(conn.execute(select(fx_rates.c.rate_date, fx_rates.c.usd_dop)).all())
    assert len(rates) == 4 and rates[date(2026, 10, 4)] == Decimal("60.1698")


def test_suspended_supplier_goes_to_review(registry, tmp_path):
    ref = make_engine(f"sqlite:///{tmp_path / 'ref.db'}")
    load_rnc_registry(ref, list(parse_rnc_lines([dgii_line("131000002", "EMPRESA ALPHA", "ACTIVO"),
                                                 dgii_line("401506254", "DISTRIBUIDORA", "SUSPENDIDO"),
                                                 dgii_line("101005009", "CLIENTE", "ACTIVO")])), min_rows=1)
    ctx = make_ctx(registry, "alpha", powerbi=FakePowerBI({"VENTAS": 11800.0}))
    ctx.__dict__["reference_engine"] = ref
    drop_ecf(registry, "alpha", "ecf_sale.xml", "ecf_purchase.xml")   # purchase is from the suspended RNC
    result = run_nightly(ctx, DAY, ConsoleNotifier(), reader=fake_reader)
    assert result["stats"]["accepted"] == 1 and result["stats"]["review"] == 1


@pytest.mark.parametrize("text,start,end", [
    ("Compra RD$61.10 Venta RD$61.55, del 31 de octubre al 2 de noviembre de 2026", date(2026, 10, 31), date(2026, 11, 2)),
    ("Compra RD$60.2 Venta RD$60.7 válidas del 3 al 5 de octubre de 2026", date(2026, 10, 3), date(2026, 10, 5)),
    ("Compra RD$62.00 Venta RD$62.40 del 31 de diciembre de 2026 al 2 de enero de 2027", date(2026, 12, 31), date(2027, 1, 2)),
    ("Según resolución de fecha 12 de marzo de 2004. Compra RD$60.1698 Venta RD$60.6902. "
     "Santo Domingo, 2 de octubre de 2026, válida hasta el día 5 de octubre de 2026", date(2026, 10, 2), date(2026, 10, 5)),
])
def test_bcrd_validity_ranges(text, start, end):
    n = parse_bcrd_notice(text, today=start)
    assert (n["from"], n["to"]) == (start, end)


def test_manual_rate_is_never_overwritten(registry):
    from backbone.reference import save_manual_rate
    ctx = make_ctx(registry, "alpha")
    with ctx.engine.begin() as conn:
        save_manual_rate(conn, date(2026, 10, 4), Decimal("59.0000"))
        save_rate(conn, {"buy": Decimal("60.1"), "sell": Decimal("60.6"), "from": date(2026, 10, 3), "to": date(2026, 10, 5)})
        save_manual_rate(conn, date(2026, 10, 4), Decimal("59.5000"))          # re-entering by hand updates
    with ctx.engine.connect() as conn:
        rates = dict(conn.execute(select(fx_rates.c.rate_date, fx_rates.c.usd_dop)).all())
    assert rates[date(2026, 10, 4)] == Decimal("59.5000") and rates[date(2026, 10, 5)] == Decimal("60.6000")


def test_registry_missing_can_be_approved_and_cedula_buyers_are_skipped():
    from backbone.validation import ValidationContext, validate_invoice
    ctx = ValidationContext(today=date(2026, 10, 4), rnc_registry={"131000002": "ACTIVO"})
    sale_to_person = dict(direction="sale", issuer_rnc="131000002", buyer_rnc="00113918205", ncf="B0200000009",
                          issue_date=date(2026, 10, 2), currency="DOP", subtotal=Decimal("100"), itbis=Decimal("18"),
                          total=Decimal("118"))
    assert validate_invoice(sale_to_person, ctx).status == "accepted"     # cédula buyer not in registry: fine
    new_supplier = dict(sale_to_person, direction="purchase", issuer_rnc="401506254", buyer_rnc="131000002")
    d = validate_invoice(new_supplier, ctx)
    assert d.status == "review" and [r.rule for r in d.problems()] == ["rnc_registry_missing"]
