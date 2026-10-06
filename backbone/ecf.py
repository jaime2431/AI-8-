"""Reader for DGII e-CF XML (electronic invoices, Ley 32-23).

e-CF is the cleanest source we have: structured, signed and already accepted by
DGII. This parser reads the header and totals; check field names against the
current DGII XSD ("Formato Comprobante Fiscal Electronico") before production.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal, InvalidOperation

from .validation import invoice_direction


class ECFError(ValueError):
    pass


class NotAnInvoice(ECFError):
    """A DGII file that is not an invoice (acknowledgments, approvals, consumer summaries). Skipped, not an error."""


# Root elements DGII uses for files that are not invoices
NON_INVOICE_ROOTS = {"ARECF": "acuse de recibo", "ACECF": "aprobacion comercial", "ANECF": "anulacion",
                     "RFCE": "resumen factura de consumo"}


def _strip_ns(root: ET.Element) -> None:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


def _text(root: ET.Element, path: str) -> str | None:
    el = root.find(path)
    return el.text.strip() if el is not None and el.text and el.text.strip() else None


def _dec(root: ET.Element, path: str) -> Decimal | None:
    t = _text(root, path)
    if t is None:
        return None
    try:
        return Decimal(t)
    except InvalidOperation:
        raise ECFError(f"{path} is not a number: {t!r}") from None


def parse_ecf(xml_bytes: bytes, own_rnc: str) -> dict:
    """Returns one staging row. own_rnc decides whether it is a sale or a purchase."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise ECFError(f"invalid XML: {e}") from e
    _strip_ns(root)
    if root.tag in NON_INVOICE_ROOTS:
        raise NotAnInvoice(f"{root.tag} ({NON_INVOICE_ROOTS[root.tag]})")
    if root.tag != "ECF":
        found = root.find(".//ECF")
        if found is None:
            raise ECFError(f"no ECF element (root is {root.tag})")
        root = found

    enc = root.find("Encabezado")
    if enc is None:
        raise ECFError("no Encabezado")

    encf = _text(enc, "IdDoc/eNCF")
    issuer = _text(enc, "Emisor/RNCEmisor")
    buyer = _text(enc, "Comprador/RNCComprador")
    raw_date = _text(enc, "Emisor/FechaEmision")
    issue_date = datetime.strptime(raw_date, "%d-%m-%Y").date() if raw_date else None

    # Totales sits inside Encabezado in the DGII format; fall back to anywhere in the document
    tot = enc.find("Totales")
    if tot is None:
        tot = root.find(".//Totales")
    if tot is None:
        raise ECFError("no Totales")
    enc_tot = ET.Element("wrap")
    enc_tot.append(tot)
    gravado = _dec(enc_tot, "Totales/MontoGravadoTotal") or Decimal(0)
    exento = _dec(enc_tot, "Totales/MontoExento") or Decimal(0)
    itbis = _dec(enc_tot, "Totales/TotalITBIS") or Decimal(0)
    total = _dec(enc_tot, "Totales/MontoTotal")
    extra = _dec(enc_tot, "Totales/MontoImpuestoAdicional") or Decimal(0)
    if total is None:
        raise ECFError("no MontoTotal")

    # Expected ITBIS by rate bucket when the split is present (I1 = 18%, I2 = 16%, I3 = 0%)
    g1, g2 = _dec(enc_tot, "Totales/MontoGravadoI1"), _dec(enc_tot, "Totales/MontoGravadoI2")
    expected = None
    if g1 is not None or g2 is not None:
        expected = ((g1 or 0) * Decimal("0.18") + (g2 or 0) * Decimal("0.16")).quantize(Decimal("0.01"))

    # In e-CF the Totales block is always in pesos; a foreign-currency invoice adds an OtraMoneda block
    # with the currency and rate. We keep pesos as the booked amount and record the foreign currency for reference.
    foreign_currency = _text(enc, "OtraMoneda/TipoMoneda")
    foreign_rate = _dec(enc, "OtraMoneda/TipoCambio")
    foreign_total = _dec(enc, "OtraMoneda/MontoTotalOtraMoneda")

    lines = []
    for item in root.findall("DetallesItems/Item"):
        lines.append({
            "description": _text(item, "NombreItem"),
            "quantity": _text(item, "CantidadItem"),
            "unit_price": _text(item, "PrecioUnitarioItem"),
            "amount": _text(item, "MontoItem"),
        })

    direction = invoice_direction(issuer, encf, own_rnc)

    return {
        "direction": direction,
        "issuer_rnc": issuer,
        "issuer_name": _text(enc, "Emisor/RazonSocialEmisor"),
        "buyer_rnc": buyer,
        "ncf": encf,
        "ncf_modified": _text(root, "InformacionReferencia/NCFModificado"),
        "issue_date": issue_date,
        "currency": "DOP",
        "foreign_currency": foreign_currency,
        "foreign_rate": foreign_rate,
        "foreign_total": foreign_total,
        "subtotal": gravado + exento,
        "taxable_amount": gravado,
        "itbis": itbis,
        "expected_itbis": expected,
        "other_taxes": extra,
        "tip": Decimal(0),
        "total": total,
        "lines": lines,
        "confidence": {},      # structured source: no extraction uncertainty
    }
