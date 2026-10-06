"""Nightly quality check. Runs after each Power BI refresh:

1. Compares numbers in Power BI with the same numbers in the clean database (QACheck list per client).
2. Checks that today's volume is normal for that weekday.
3. Asks Claude to write a short Spanish summary for the owner (deterministic text if Claude is unavailable).

If any check fails the morning summary is HELD and our team is alerted.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import and_, func, select
from sqlalchemy.engine import Connection

from ..db import clean_invoices as ci

VOLUME_BAND = Decimal("0.40")     # starting setting: +/-40% vs same-weekday average, tune per client


@dataclass
class CheckResult:
    name: str
    ok: bool
    db_value: str | None = None
    pbi_value: str | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


DB_METRICS = {
    "sales_total_dop": lambda day: select(func.coalesce(func.sum(ci.c.total_dop), 0)).where(
        and_(ci.c.direction == "sale", ci.c.issue_date == day)),
    "purchases_total_dop": lambda day: select(func.coalesce(func.sum(ci.c.total_dop), 0)).where(
        and_(ci.c.direction == "purchase", ci.c.issue_date == day)),
    "invoice_count": lambda day: select(func.count()).select_from(ci).where(ci.c.issue_date == day),
    "itbis_sales_dop": lambda day: select(func.coalesce(func.sum(ci.c.itbis * func.coalesce(ci.c.fx_rate, 1)), 0)).where(
        and_(ci.c.direction == "sale", ci.c.issue_date == day)),
}


def db_metric(conn: Connection, metric: str, day: date) -> Decimal:
    if metric not in DB_METRICS:
        raise KeyError(f"unknown metric {metric}")
    return Decimal(str(conn.execute(DB_METRICS[metric](day)).scalar() or 0))


def dax_for(template: str, day: date) -> str:
    return template.replace("{date}", f"DATE({day.year}, {day.month}, {day.day})")


def _first_value(rows: list[dict]) -> Decimal:
    if not rows:
        return Decimal(0)
    v = next(iter(rows[0].values()))
    return Decimal(str(v)) if v is not None else Decimal(0)


def compare_with_powerbi(conn: Connection, powerbi, checks, day: date) -> list[CheckResult]:
    out = []
    for chk in checks:
        dbv = db_metric(conn, chk.db_metric, day)
        try:
            pbv = _first_value(powerbi.execute_dax(dax_for(chk.dax, day)))
        except Exception as e:  # a failed query is a failed check, not a crash
            out.append(CheckResult(chk.name, False, str(dbv), None, f"Power BI query failed: {e}"))
            continue
        base = max(abs(dbv), Decimal(1))
        diff_pct = abs(dbv - pbv) / base * 100
        ok = diff_pct <= Decimal(str(chk.tolerance_pct))
        out.append(CheckResult(chk.name, ok, str(dbv), str(pbv), "" if ok else f"differs by {diff_pct:.2f}%"))
    return out


def volume_check(conn: Connection, day: date, weeks: int = 8) -> CheckResult:
    count = lambda d: conn.execute(select(func.count()).select_from(ci).where(ci.c.issue_date == d)).scalar() or 0
    today = count(day)
    history = [count(day - timedelta(weeks=w)) for w in range(1, weeks + 1)]
    history = [h for h in history if h > 0]
    if len(history) < 3:
        return CheckResult("normal_volume", True, str(today), None, "not enough history yet")
    avg = Decimal(sum(history)) / len(history)
    low, high = avg * (1 - VOLUME_BAND), avg * (1 + VOLUME_BAND)
    ok = low <= today <= high
    return CheckResult("normal_volume", ok, str(today), f"{avg:.1f}",
                       "" if ok else f"{today} invoices vs usual {avg:.0f} for this weekday")


def write_summary(claude, model: str, client_name: str, day: date, results: list[CheckResult], stats: dict,
                  conn: Connection) -> str:
    sales = db_metric(conn, "sales_total_dop", day)
    purchases = db_metric(conn, "purchases_total_dop", day)
    facts = (f"Empresa: {client_name}\nFecha: {day.isoformat()}\nVentas: RD${sales:,.2f}\n"
             f"Compras: RD${purchases:,.2f}\nFacturas cargadas hoy: {stats.get('accepted', 0)}\n"
             f"Pendientes de revisión: {stats.get('review', 0)}")
    fallback = f"Resumen del {day.isoformat()} — {client_name}\n{facts}"
    if claude is None:
        return fallback
    try:
        resp = claude.messages.create(
            model=model, max_tokens=400,
            system=("Escribe un resumen breve (máximo 6 líneas) en español para el dueño de una PyME dominicana. "
                    "Usa solo los datos dados. No inventes cifras ni causas."),
            messages=[{"role": "user", "content": facts}],
        )
        text = "".join(getattr(b, "text", "") for b in resp.content).strip()
        return text or fallback
    except Exception:
        return fallback
