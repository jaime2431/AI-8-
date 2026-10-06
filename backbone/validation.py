"""Validation rules. Plain code, no AI: the same row always gets the same decision.

Every row from every source (e-CF, ERP, Excel, Claude-read documents) passes
through validate_invoice() before it can reach the clean tables.

Outcomes per rule:  pass | review (send to review queue) | reject (never load)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

TOLERANCE = Decimal("1.00")          # pesos of rounding allowed in totals
ITBIS_RATES = (Decimal("0.18"), Decimal("0.16"))

# NCF types (traditional, 11 chars: B + type + 8 digits) and e-NCF types (13 chars: E + type + 10 digits)
NCF_TYPES = {"01", "02", "03", "04", "11", "12", "13", "14", "15", "16", "17"}
ENCF_TYPES = {"31", "32", "33", "34", "41", "43", "44", "45", "46", "47"}

REQUIRED = ("direction", "issuer_rnc", "ncf", "issue_date", "currency", "subtotal", "total")
CONFIDENCE_FIELDS = ("issuer_rnc", "ncf", "issue_date", "subtotal", "itbis", "total")


@dataclass
class RuleResult:
    rule: str
    outcome: str            # pass | review | reject
    message: str = ""


@dataclass
class Decision:
    status: str             # accepted | review | rejected
    results: list[RuleResult]

    def problems(self) -> list[RuleResult]:
        return [r for r in self.results if r.outcome != "pass"]

    def reason(self) -> str:
        return "; ".join(f"{r.rule}: {r.message}" for r in self.problems())


@dataclass
class ValidationContext:
    today: date
    open_period_start: date | None = None
    confidence_threshold: float = 0.90
    existing_keys: set[tuple[str, str]] = field(default_factory=set)   # (issuer_rnc, ncf) already loaded
    fx_rates: dict[date, Decimal] = field(default_factory=dict)
    rnc_registry: dict[str, str] | None = None  # {rnc: DGII status} for the RNCs in this batch; None = not loaded
    known_suppliers: set[str] | None = None     # None = do not require known suppliers


# ---------- helpers ----------

def to_decimal(v) -> Decimal | None:
    if v is None or v == "":
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v).replace(",", ""))
    except InvalidOperation:
        return None


def clean_rnc(v) -> str | None:
    if v is None:
        return None
    digits = re.sub(r"\D", "", str(v))
    return digits or None


def rnc_checksum_ok(rnc: str) -> bool:
    """DGII check digit for 9-digit company RNCs; Luhn for 11-digit cédulas."""
    if len(rnc) == 9:
        weights = (7, 9, 8, 6, 5, 4, 3, 2)
        s = sum(int(d) * w for d, w in zip(rnc[:8], weights))
        r = s % 11
        check = 2 if r == 0 else 1 if r == 1 else 11 - r
        return check == int(rnc[8])
    if len(rnc) == 11:
        total = 0
        for i, d in enumerate(rnc):
            n = int(d) * (1 if i % 2 == 0 else 2)
            total += n - 9 if n > 9 else n
        return total % 10 == 0
    return False


def normalize_ncf(v) -> str | None:
    if v is None:
        return None
    s = re.sub(r"[\s\-]", "", str(v)).upper()
    return s or None


def ncf_type(ncf: str) -> str | None:
    """Returns e.g. 'B01' or 'E31' if the NCF is well formed, else None."""
    if re.fullmatch(r"B\d{10}", ncf) and ncf[1:3] in NCF_TYPES:
        return ncf[:3]
    if re.fullmatch(r"E\d{12}", ncf) and ncf[1:3] in ENCF_TYPES:
        return ncf[:3]
    return None


# ---------- the rules ----------

def validate_invoice(row: dict, ctx: ValidationContext) -> Decision:
    res: list[RuleResult] = []
    add = lambda rule, outcome, msg="": res.append(RuleResult(rule, outcome, msg))

    # 1. required fields
    missing = [f for f in REQUIRED if row.get(f) in (None, "")]
    add("required_fields", "review" if missing else "pass", f"missing {', '.join(missing)}" if missing else "")

    if row.get("direction") not in (None, "", "sale", "purchase"):
        add("direction_valid", "review", f"direction must be sale or purchase, got {row.get('direction')!r}")

    issuer = clean_rnc(row.get("issuer_rnc"))
    buyer = clean_rnc(row.get("buyer_rnc"))
    ncf = normalize_ncf(row.get("ncf")) or ""

    # 2. RNC valid (format, check digit, DGII registry when loaded)
    for label, rnc in (("issuer_rnc", issuer), ("buyer_rnc", buyer)):
        if rnc is None:
            continue
        if len(rnc) not in (9, 11):
            add(f"{label}_valid", "review", f"{rnc} must have 9 or 11 digits")
        elif not rnc_checksum_ok(rnc):
            add(f"{label}_valid", "review", f"{rnc} fails the check digit")
        else:
            # Registry check: skipped for cédulas of buyers (individuals are often not registered).
            check_registry = ctx.rnc_registry is not None and not (label == "buyer_rnc" and len(rnc) == 11)
            if check_registry and rnc not in ctx.rnc_registry:
                add(f"{label}_valid", "pass")
                add("rnc_registry_missing", "review", f"{rnc} not found in DGII registry (new or not registered)")
            elif check_registry and ctx.rnc_registry[rnc] != "ACTIVO":
                add(f"{label}_valid", "review", f"{rnc} has DGII status {ctx.rnc_registry[rnc]}")
            else:
                add(f"{label}_valid", "pass")

    # 3. NCF / e-NCF format
    if ncf:
        add("ncf_format", "pass" if ncf_type(ncf) else "review", "" if ncf_type(ncf) else f"{ncf} is not a valid NCF/e-NCF")

    # 4. totals add up
    sub, itbis, total = to_decimal(row.get("subtotal")), to_decimal(row.get("itbis")) or Decimal(0), to_decimal(row.get("total"))
    other, tip = to_decimal(row.get("other_taxes")) or Decimal(0), to_decimal(row.get("tip")) or Decimal(0)
    if sub is not None and total is not None:
        diff = abs(sub + itbis + other + tip - total)
        add("totals_add_up", "pass" if diff <= TOLERANCE else "review",
            "" if diff <= TOLERANCE else f"subtotal+ITBIS+other+tip differs from total by {diff}")

    # 5. ITBIS rate plausible
    taxable = to_decimal(row.get("taxable_amount"))
    expected = to_decimal(row.get("expected_itbis"))
    if expected is not None:
        ok = abs(itbis - expected) <= TOLERANCE
        add("itbis_rate", "pass" if ok else "review", "" if ok else f"ITBIS {itbis} vs expected {expected}")
    elif taxable is not None:
        ok = itbis == 0 or any(abs(itbis - taxable * r) <= TOLERANCE for r in ITBIS_RATES)
        add("itbis_rate", "pass" if ok else "review", "" if ok else f"ITBIS {itbis} is not 18%/16% of {taxable}")
    elif sub is not None:
        ok = itbis <= sub * Decimal("0.18") + TOLERANCE
        add("itbis_rate", "pass" if ok else "review", "" if ok else f"ITBIS {itbis} above 18% of subtotal")

    # 6. duplicates -> reject
    if issuer and ncf and (issuer, ncf) in ctx.existing_keys:
        add("no_duplicate", "reject", f"{issuer}/{ncf} already loaded")
    else:
        add("no_duplicate", "pass")

    # 7. dates make sense
    d = row.get("issue_date")
    if isinstance(d, str) and d:
        try:
            d = date.fromisoformat(d)
        except ValueError:
            add("date_valid", "review", f"unreadable date {d}")
            d = None
    if isinstance(d, date):
        if d > ctx.today:
            add("date_valid", "review", f"{d} is in the future")
        elif ctx.open_period_start and d < ctx.open_period_start:
            add("date_valid", "review", f"{d} is before the open period ({ctx.open_period_start})")
        else:
            add("date_valid", "pass")

    # 8. currency
    cur = (row.get("currency") or "").upper()
    if cur == "DOP":
        add("currency", "pass")
    elif cur == "USD":
        ok = isinstance(d, date) and d in ctx.fx_rates
        add("currency", "pass" if ok else "review", "" if ok else f"no Banco Central USD rate for {d}")
    elif cur:
        add("currency", "review", f"unsupported currency {cur}")

    # 9. known supplier (only if the client asked for it)
    if ctx.known_suppliers is not None and row.get("direction") == "purchase" and issuer:
        ok = issuer in ctx.known_suppliers
        add("known_supplier", "pass" if ok else "review", "" if ok else f"new supplier {issuer}: map it first")

    # 10. extraction confidence (documents read by Claude)
    conf = row.get("confidence") or {}

    def _low(v) -> bool:
        try:
            return float(v) < ctx.confidence_threshold
        except (TypeError, ValueError):
            return True     # unreadable confidence counts as low

    low = [f for f in CONFIDENCE_FIELDS if f in conf and conf[f] is not None and _low(conf[f])]
    add("confidence", "review" if low else "pass", f"low confidence on {', '.join(low)}" if low else "")

    outcomes = {r.outcome for r in res}
    status = "rejected" if "reject" in outcomes else "review" if "review" in outcomes else "accepted"
    return Decision(status, res)
