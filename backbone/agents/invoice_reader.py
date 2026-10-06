"""Claude invoice reader: turns a PDF or photo of a Dominican invoice into one
staging row with a confidence score per field. It never writes to Power BI and
never decides whether the row is valid; validation.py does that.
"""
from __future__ import annotations

import base64
import re
from decimal import Decimal

SYSTEM = (
    "Eres un asistente contable en República Dominicana. Lees facturas (comprobantes fiscales) "
    "y registras sus datos exactamente como aparecen impresos. Si un dato no se ve con claridad, "
    "devuélvelo como null y baja su confianza. Nunca inventes ni calcules valores que no estén impresos."
)

INVOICE_TOOL = {
    "name": "record_invoice",
    "description": "Registra los campos de una factura dominicana tal como aparecen impresos.",
    "input_schema": {
        "type": "object",
        "properties": {
            "issuer_rnc": {"type": ["string", "null"], "description": "RNC o cédula de quien emite la factura"},
            "issuer_name": {"type": ["string", "null"]},
            "buyer_rnc": {"type": ["string", "null"], "description": "RNC del comprador si aparece"},
            "ncf": {"type": ["string", "null"], "description": "NCF o e-NCF, p. ej. B0100000123 o E310000000123"},
            "issue_date": {"type": ["string", "null"], "description": "Fecha de emisión en formato YYYY-MM-DD"},
            "currency": {"type": ["string", "null"], "enum": ["DOP", "USD", None]},
            "subtotal": {"type": ["number", "null"]},
            "taxable_amount": {"type": ["number", "null"], "description": "Monto gravado, si aparece"},
            "itbis": {"type": ["number", "null"]},
            "other_taxes": {"type": ["number", "null"], "description": "ISC u otros impuestos"},
            "tip": {"type": ["number", "null"], "description": "Propina legal (10%) si aparece"},
            "total": {"type": ["number", "null"]},
            "lines": {
                "type": "array",
                "items": {"type": "object", "properties": {
                    "description": {"type": "string"}, "quantity": {"type": ["number", "null"]},
                    "unit_price": {"type": ["number", "null"]}, "amount": {"type": ["number", "null"]}}},
            },
            "confidence": {
                "type": "object",
                "description": "Confianza de 0 a 1 para cada campo leído",
                "properties": {k: {"type": "number"} for k in
                               ("issuer_rnc", "ncf", "issue_date", "subtotal", "itbis", "total")},
            },
        },
        "required": ["issuer_rnc", "ncf", "issue_date", "currency", "subtotal", "itbis", "total", "confidence"],
    },
}

IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


class ExtractionError(RuntimeError):
    pass


def _content_block(data: bytes, media_type: str) -> dict:
    b64 = base64.standard_b64encode(data).decode()
    if media_type == "application/pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": media_type, "data": b64}}
    if media_type in IMAGE_TYPES:
        return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}}
    raise ExtractionError(f"unsupported file type {media_type}")


def _num(v):
    return None if v is None else Decimal(str(v))


def normalize(fields: dict, direction: str = "purchase") -> dict:
    digits = lambda v: re.sub(r"\D", "", v) if isinstance(v, str) else None
    return {
        "direction": direction,
        "issuer_rnc": digits(fields.get("issuer_rnc")),
        "issuer_name": fields.get("issuer_name"),
        "buyer_rnc": digits(fields.get("buyer_rnc")),
        "ncf": (fields.get("ncf") or "").replace(" ", "").replace("-", "").upper() or None,
        "issue_date": fields.get("issue_date"),
        "currency": (fields.get("currency") or "DOP").upper(),
        "subtotal": _num(fields.get("subtotal")),
        "taxable_amount": _num(fields.get("taxable_amount")),
        "itbis": _num(fields.get("itbis")),
        "other_taxes": _num(fields.get("other_taxes")),
        "tip": _num(fields.get("tip")),
        "total": _num(fields.get("total")),
        "lines": fields.get("lines") or [],
        "confidence": fields.get("confidence") or {},
    }


def read_document(claude, model: str, data: bytes, media_type: str, direction: str = "purchase") -> dict:
    """claude = that client's anthropic.Anthropic instance (its own API key / Console workspace)."""
    resp = claude.messages.create(
        model=model,
        max_tokens=4096,
        system=SYSTEM,
        tools=[INVOICE_TOOL],
        tool_choice={"type": "tool", "name": "record_invoice"},
        messages=[{"role": "user", "content": [
            _content_block(data, media_type),
            {"type": "text", "text": "Registra los datos de esta factura."},
        ]}],
    )
    usage = getattr(resp, "usage", None)
    if usage is not None and hasattr(claude, "_datia_usage"):
        claude._datia_usage.append(("invoice_reader", getattr(usage, "input_tokens", 0), getattr(usage, "output_tokens", 0)))
    if getattr(resp, "stop_reason", None) == "max_tokens":
        raise ExtractionError("answer was cut off (too many lines); split the document or raise max_tokens")
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "record_invoice":
            return normalize(block.input, direction)
    raise ExtractionError("Claude did not return invoice fields")
