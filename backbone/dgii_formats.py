"""DGII Formatos de Envío 606 (compras) and 607 (ventas) built from clean_invoices.

The layout copies what DGII's own Excel tools write when you press "Generar Archivo"
(Formato de Envío 606 and 607, NG 07-2018 y 05-2019, downloaded from
dgii.gov.do/herramientas/formularios/formatoEnvioDatos):

  header   606|<RNC>|<AAAAMM>|<cantidad registros>        (same for 607)
  detail   23 pipe-separated fields, CRLF between lines, no line break after the last one
  file     DGII_F_606_<RNC>_<AAAAMM>.TXT

Amounts are DOP with a decimal point and 2 decimals, no thousands separator. Credit notes
are stored negative in clean_invoices; DGII wants them positive (it subtracts type 04 itself).

We never invent data. Fields DGII lets you leave empty (fecha de pago, retenciones,
percepciones...) are left empty. Required fields we do not hold (tipo de bienes y servicios,
forma de pago, tipo de ingreso) are left empty with a warning per row, unless the operator
passes the code explicitly; DGII's tools have no default for them.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from .validation import ncf_type

CENT = Decimal("0.01")
CREDIT_NOTES = {"B04", "E34"}
DEBIT_NOTES = {"B03", "E33"}
CONSUMER = {"B02", "E32"}
# Comprobantes the buyer issues itself (compras, gastos menores, pagos al exterior): they are purchases even
# though issuer_rnc is our own RNC. In 13 and 17 DGII wants our own RNC as the supplier id.
SELF_ISSUED_PURCHASE = {"B11", "E41", "B13", "E43", "B17", "E47"}
OWN_ID_TYPES = {"B13", "E43", "B17", "E47"}
CONSUMER_DETAIL_MIN = Decimal("250000")      # NG 10-18: facturas de consumo below this go only in the OV summary
MAX_RECORDS = {"606": 10000, "607": 65000}   # limits stated in the instructivos

TIPO_BIENES = {f"{i:02d}" for i in range(1, 12)}      # 01 gastos de personal .. 11 gastos de seguros
FORMA_PAGO_606 = {f"{i:02d}" for i in range(1, 8)}    # 01 efectivo .. 07 mixto
TIPO_INGRESO = {str(i) for i in range(1, 7)}           # 1 operaciones (no financieros) .. 6 otros ingresos


def parse_period(period: str) -> tuple[int, int]:
    """'2026-09' or '202609' -> (2026, 9)."""
    s = str(period).replace("-", "")
    if len(s) != 6 or not s.isdigit() or not 1 <= int(s[4:]) <= 12:
        raise ValueError(f"period must be YYYY-MM or YYYYMM, got {period!r}")
    return int(s[:4]), int(s[4:])


def period_bounds(period: str) -> tuple[date, date]:
    """First day of the month and first day of the next month (exclusive end)."""
    y, m = parse_period(period)
    return date(y, m, 1), date(y + (m == 12), m % 12 + 1, 1)


def file_name(fmt: str, own_rnc: str, period: str) -> str:
    y, m = parse_period(period)
    return f"DGII_F_{fmt}_{_digits(own_rnc)}_{y}{m:02d}.TXT"


def _digits(v) -> str:
    return "".join(ch for ch in str(v or "") if ch.isdigit())


def _id_type(rnc: str) -> str:
    return {9: "1", 11: "2"}.get(len(rnc), "")       # 1 = RNC, 2 = cédula


def _money(v: Decimal | None) -> str:
    return "" if v is None else f"{abs(v).quantize(CENT)}"


def _to_dop(r, field: str) -> Decimal | None:
    """Amount in pesos. USD rows keep the original amounts plus fx_rate (total_dop is only the total)."""
    v = r.get(field)
    if v is None:
        return None
    v = Decimal(str(v))
    if (r.get("currency") or "DOP").upper() == "DOP":
        return v.quantize(CENT)
    return (v * Decimal(str(r["fx_rate"]))).quantize(CENT)


def _common(r, fmt: str, warnings: list[str]):
    """Checks shared by both formats. Returns (ncf, type, ncf_modified) or None if the row cannot be reported."""
    ncf = r.get("ncf") or ""
    kind = ncf_type(ncf) if ncf else None
    tag = ncf or f"clean id {r.get('id')}"
    if kind is None:
        warnings.append(f"{tag}: not a valid NCF/e-NCF, left out of the {fmt}")
        return None
    if (r.get("currency") or "DOP").upper() != "DOP" and r.get("fx_rate") is None:
        warnings.append(f"{ncf}: {r.get('currency')} invoice without an exchange rate, left out of the {fmt}")
        return None
    modified = r.get("ncf_modified") or ""
    if kind in CREDIT_NOTES | DEBIT_NOTES and not modified:
        warnings.append(f"{ncf}: {'credit' if kind in CREDIT_NOTES else 'debit'} note without the modified NCF; "
                        f"field 'NCF modificado' left empty")
    return ncf, kind, modified


def _rows_in_period(rows, period: str, warnings: list[str]):
    start, end = period_bounds(period)
    for r in rows:
        d = r.get("issue_date")
        if d is None:
            warnings.append(f"{r.get('ncf') or r.get('id')}: no issue date, left out")
        elif start <= d < end:
            yield r


def _finish(fmt: str, own: str, period: str, lines: list[list[str]], warnings: list[str]) -> tuple[str, list[str]]:
    y, m = parse_period(period)
    if len(lines) > MAX_RECORDS[fmt]:
        warnings.append(f"{len(lines)} records: DGII accepts at most {MAX_RECORDS[fmt]} per {fmt} file")
    out = [f"{fmt}|{own}|{y}{m:02d}|{len(lines)}"] + ["|".join(f) for f in lines]
    return "\r\n".join(out), warnings      # DGII's tool writes CRLF and no break after the last line


def _check_own(own_rnc: str) -> str:
    own = _digits(own_rnc)
    if len(own) not in (9, 11):
        raise ValueError(f"own RNC must be 9 (RNC) or 11 (cédula) digits, got {own_rnc!r}")
    return own


# ---------- 606: compras ----------

def build_606(rows, own_rnc: str, period: str, tipo_bienes: str | None = None,
              forma_pago: str | None = None) -> tuple[str, list[str]]:
    """Formato 606. tipo_bienes / forma_pago: codes the operator states for the whole file (e.g. '09', '02');
    without them those required fields are left empty and every row gets a warning."""
    own = _check_own(own_rnc)
    if tipo_bienes is not None and tipo_bienes not in TIPO_BIENES:
        raise ValueError(f"tipo de bienes y servicios must be 01..11, got {tipo_bienes!r}")
    if forma_pago is not None and forma_pago not in FORMA_PAGO_606:
        raise ValueError(f"forma de pago must be 01..07, got {forma_pago!r}")
    warnings: list[str] = []
    lines = []
    for r in _rows_in_period(rows, period, warnings):
        kind = ncf_type(r.get("ncf") or "")
        issuer, buyer = _digits(r.get("issuer_rnc")), _digits(r.get("buyer_rnc"))
        if kind in SELF_ISSUED_PURCHASE and issuer == own:
            supplier = own if kind in OWN_ID_TYPES else buyer
        elif r.get("direction") == "purchase":
            if buyer and buyer != own:
                warnings.append(f"{r.get('ncf')}: purchase made out to RNC {buyer}, not {own}; left out")
                continue
            supplier = issuer
        else:
            continue
        common = _common(r, "606", warnings)
        if common is None:
            continue
        ncf, kind, modified = common
        id_type = _id_type(supplier)
        if not id_type:
            warnings.append(f"{ncf}: supplier RNC/cédula missing or malformed ({supplier or 'empty'})")
        subtotal, itbis = _to_dop(r, "subtotal"), _to_dop(r, "itbis")
        if subtotal is None:
            warnings.append(f"{ncf}: no subtotal; 'Total Monto Facturado' left empty")
        if itbis is None:
            warnings.append(f"{ncf}: no ITBIS; 'ITBIS Facturado' left empty")
        # Bienes vs servicios split is not in the clean data: both left empty, the known total goes in field 10
        warnings.append(f"{ncf}: monto en servicios / monto en bienes split unknown; fields 8 and 9 left empty")
        if tipo_bienes is None:
            warnings.append(f"{ncf}: tipo de bienes y servicios comprados (required) not in our data; left empty")
        if forma_pago is None:
            warnings.append(f"{ncf}: forma de pago (required) not in our data; left empty")
        other, tip = _to_dop(r, "other_taxes"), _to_dop(r, "tip")
        lines.append([
            supplier,                                    # 1  RNC o cédula del suplidor
            id_type,                                     # 2  tipo id: 1 RNC, 2 cédula
            tipo_bienes or "",                           # 3  tipo de bienes y servicios comprados
            ncf,                                         # 4  NCF / e-NCF
            modified,                                    # 5  NCF o documento modificado
            r["issue_date"].strftime("%Y%m%d"),          # 6  fecha comprobante AAAAMMDD
            "",                                          # 7  fecha pago (optional; not in our data)
            "",                                          # 8  monto facturado en servicios
            "",                                          # 9  monto facturado en bienes
            _money(subtotal),                            # 10 total monto facturado (sin impuestos)
            _money(itbis),                               # 11 ITBIS facturado
            "",                                          # 12 ITBIS retenido
            "",                                          # 13 ITBIS sujeto a proporcionalidad (Art. 349)
            "",                                          # 14 ITBIS llevado al costo
            _money(itbis),                               # 15 ITBIS por adelantar = 11 - 14 (DGII's own formula)
            "",                                          # 16 ITBIS percibido en compras
            "",                                          # 17 tipo de retención en ISR
            "",                                          # 18 monto retención renta
            "",                                          # 19 ISR percibido en compras
            "",                                          # 20 impuesto selectivo al consumo (not split out)
            _money(other) if other else "",              # 21 otros impuestos/tasas
            _money(tip) if tip else "",                  # 22 monto propina legal
            forma_pago or "",                            # 23 forma de pago
        ])
    return _finish("606", own, period, lines, warnings)


# ---------- 607: ventas ----------

def build_607(rows, own_rnc: str, period: str, tipo_ingreso: str | None = None) -> tuple[str, list[str]]:
    """Formato 607. tipo_ingreso: code (1..6) the operator states for the whole file; without it the
    required field is left empty and every row gets a warning.

    Left out of the detail, with a summary warning:
    - facturas de consumo (B02/E32) under RD$250,000: they go in the OV "Resumen General de Facturas de Consumo";
    - e-CF (E..): DGII exempts 100% electronic issuers from the 607 and its tool only accepts 11/19-char NCF.
    """
    own = _check_own(own_rnc)
    if tipo_ingreso is not None and tipo_ingreso not in TIPO_INGRESO:
        raise ValueError(f"tipo de ingreso must be 1..6, got {tipo_ingreso!r}")
    warnings: list[str] = []
    lines = []
    consumer_n, consumer_amount, consumer_itbis = 0, Decimal(0), Decimal(0)
    ecf_n = 0
    for r in _rows_in_period(rows, period, warnings):
        if r.get("direction") != "sale" or ncf_type(r.get("ncf") or "") in SELF_ISSUED_PURCHASE:
            continue
        issuer = _digits(r.get("issuer_rnc"))
        if issuer and issuer != own:
            warnings.append(f"{r.get('ncf')}: sale issued by RNC {issuer}, not {own}; left out")
            continue
        common = _common(r, "607", warnings)
        if common is None:
            continue
        ncf, kind, modified = common
        if kind.startswith("E"):
            ecf_n += 1
            continue
        subtotal, itbis = _to_dop(r, "subtotal"), _to_dop(r, "itbis")
        total = _to_dop(r, "total")
        if kind in CONSUMER and total is not None and abs(total) < CONSUMER_DETAIL_MIN:
            consumer_n += 1
            consumer_amount += abs(subtotal or 0)
            consumer_itbis += abs(itbis or 0)
            continue
        buyer = _digits(r.get("buyer_rnc"))
        id_type = _id_type(buyer)
        # DGII lets a note that modifies a small factura de consumo go without the buyer's id
        anonymous_ok = (kind in CREDIT_NOTES | DEBIT_NOTES and ncf_type(modified or "-") in CONSUMER
                        and subtotal is not None and abs(subtotal) < CONSUMER_DETAIL_MIN)
        if not id_type and not anonymous_ok:
            warnings.append(f"{ncf}: buyer RNC/cédula missing or malformed ({buyer or 'empty'}); required here")
        if subtotal is None:
            warnings.append(f"{ncf}: no subtotal; 'Monto Facturado' left empty")
        if itbis is None:
            warnings.append(f"{ncf}: no ITBIS; 'ITBIS Facturado' left empty")
        if tipo_ingreso is None:
            warnings.append(f"{ncf}: tipo de ingreso (required) not in our data; left empty")
        if kind not in CREDIT_NOTES:       # DGII's tool requires at least one of fields 17-23 except on credit notes
            warnings.append(f"{ncf}: forma de venta (fields 17-23, at least one required) not in our data; left empty")
        other, tip = _to_dop(r, "other_taxes"), _to_dop(r, "tip")
        lines.append([
            buyer,                                       # 1  RNC / cédula / pasaporte del cliente
            id_type,                                     # 2  tipo id: 1 RNC, 2 cédula (3 pasaporte: not stored)
            ncf,                                         # 3  NCF
            modified,                                    # 4  NCF modificado
            tipo_ingreso or "",                          # 5  tipo de ingreso (1 digit)
            r["issue_date"].strftime("%Y%m%d"),          # 6  fecha comprobante AAAAMMDD
            "",                                          # 7  fecha de retención (optional; not in our data)
            _money(subtotal),                            # 8  monto facturado (sin impuestos)
            _money(itbis),                               # 9  ITBIS facturado
            "",                                          # 10 ITBIS retenido por terceros
            "",                                          # 11 ITBIS percibido
            "",                                          # 12 retención renta por terceros
            "",                                          # 13 ISR percibido
            "",                                          # 14 impuesto selectivo al consumo (not split out)
            _money(other) if other else "",              # 15 otros impuestos/tasas
            _money(tip) if tip else "",                  # 16 monto propina legal
            "", "", "", "", "", "", "",                  # 17-23 efectivo, cheque/transf., tarjeta, crédito,
        ])                                               #       bonos, permuta, otras: not in our data
    if consumer_n:
        warnings.append(f"{consumer_n} facturas de consumo under RD$250,000 not in the file: enter them in the "
                        f"Oficina Virtual 'Resumen General de Facturas de Consumo' (monto {consumer_amount:.2f}, "
                        f"ITBIS {consumer_itbis:.2f})")
    if ecf_n:
        warnings.append(f"{ecf_n} e-CF sales not in the file: DGII exempts issuers whose invoicing is 100% electronic "
                        f"from the 607, and its 607 tool does not accept e-NCF; confirm if this client still "
                        f"issues paper NCF")
    return _finish("607", own, period, lines, warnings)
