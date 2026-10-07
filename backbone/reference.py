"""Public reference data loaded every day: the DGII RNC registry and the Banco Central USD rate.

These are public, not client data. The RNC registry lives in one shared reference
database; the USD rate is written into each client's own fx_rates table so that
every client database stays complete on its own.

Sources (verify once a quarter; government sites change):
  - DGII RNC registry: DGII_RNC.zip, a pipe-separated text file, latin-1, 11 columns per row
      0 RNC | 1 name | 2 trade name | 3 activity | ... | 8 incorporation date | 9 status | 10 regime
    Status values: ACTIVO, SUSPENDIDO, DADO DE BAJA, CESE TEMPORAL, ANULADO, RECHAZADO
  - Banco Central daily rate notice (PDF): buy and sell RD$/US$ rate and the dates it is valid for.
"""
from __future__ import annotations

import io
import re
import time
import zipfile
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, Iterator

import httpx
from sqlalchemy import Column, Date, DateTime, MetaData, String, Table, delete, insert, select, update
from sqlalchemy.engine import Connection, Engine

from .db import fx_rates, make_engine, utcnow

DGII_RNC_URL = "https://dgii.gov.do/app/WebApps/Consultas/RNC/DGII_RNC.zip"
BCRD_RATE_URL = "https://cdn.bancentral.gov.do/documents/estadisticas/mercado-cambiario/documents/tasaus_mc.pdf"

KNOWN_STATUSES = {"ACTIVO", "SUSPENDIDO", "DADO DE BAJA", "CESE TEMPORAL", "ANULADO", "RECHAZADO"}

ref_metadata = MetaData()

rnc_registry = Table(
    "rnc_registry", ref_metadata,
    Column("rnc", String(11), primary_key=True),
    Column("name", String(255)),
    Column("trade_name", String(255)),
    Column("status", String(20), nullable=False),
    Column("regime", String(60)),
    Column("loaded_at", DateTime, nullable=False, default=utcnow),
)

reference_loads = Table(
    "reference_loads", ref_metadata,
    Column("name", String(30), primary_key=True),          # "rnc_registry"
    Column("loaded_at", DateTime, nullable=False),
    Column("rows", String(20)),
)


class ReferenceFormatError(ValueError):
    pass


# ---------------- DGII RNC registry ----------------

def _collapse(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def parse_rnc_lines(lines: Iterable[str]) -> Iterator[dict]:
    """Yields one dict per valid row. Raises if most rows do not look like the DGII layout,
    so a silent format change never wipes the registry."""
    seen = recognised = 0
    pending = ""                    # the real file has line breaks inside some name fields
    for i, raw in enumerate(lines):
        line = raw.lstrip("﻿\xef\xbb\xbf").rstrip("\r\n") if i == 0 else raw.rstrip("\r\n")
        if pending:
            joined = pending + " " + line
            n = joined.count("|") + 1
            if n < 11:
                pending = joined
                continue
            pending = ""
            if n == 11:
                line = joined
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) < 11:
            pending = line
            continue
        if len(parts) != 11:
            continue
        status = _collapse(parts[9]).upper()
        rnc = re.sub(r"\D", "", parts[0])
        seen += 1
        if status in KNOWN_STATUSES:
            recognised += 1
        if seen == 1000 and recognised / seen < 0.5:
            raise ReferenceFormatError("DGII file does not match the expected 11-column layout")
        if not rnc or len(rnc) not in (9, 11):
            continue
        yield {"rnc": rnc, "name": _collapse(parts[1])[:255], "trade_name": _collapse(parts[2])[:255],
               "status": status[:20], "regime": _collapse(parts[10])[:60]}
    if seen and seen < 1000 and recognised / seen < 0.5:
        raise ReferenceFormatError("DGII file does not match the expected 11-column layout")


def read_rnc_zip(data: bytes) -> Iterator[dict]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith((".txt", ".csv"))]
        if not names:
            raise ReferenceFormatError("no text file inside DGII_RNC.zip")
        with zf.open(names[0]) as fh:
            yield from parse_rnc_lines(io.TextIOWrapper(fh, encoding="latin-1", newline=""))


def load_rnc_registry(engine: Engine, rows: Iterable[dict], min_rows: int = 100_000, batch: int = 5000) -> int:
    """Replaces the registry in one transaction. Refuses a suspiciously small file (min_rows)."""
    ref_metadata.create_all(engine)
    rows = list(rows)
    if len(rows) < min_rows:
        raise ReferenceFormatError(f"only {len(rows)} rows; refusing to replace the registry (min {min_rows})")
    with engine.begin() as conn:
        conn.execute(delete(rnc_registry))
        for i in range(0, len(rows), batch):
            conn.execute(insert(rnc_registry), rows[i:i + batch])
        conn.execute(delete(reference_loads).where(reference_loads.c.name == "rnc_registry"))
        conn.execute(insert(reference_loads).values(name="rnc_registry", loaded_at=utcnow(), rows=str(len(rows))))
    return len(rows)


def lookup_rncs(engine: Engine, rncs: Iterable[str], chunk: int = 900) -> dict[str, str]:
    """Returns {rnc: status} for the RNCs found. Only queries the RNCs in the batch being validated."""
    wanted = sorted({r for r in rncs if r})
    found: dict[str, str] = {}
    with engine.connect() as conn:
        for i in range(0, len(wanted), chunk):
            part = wanted[i:i + chunk]
            for r in conn.execute(select(rnc_registry.c.rnc, rnc_registry.c.status).where(rnc_registry.c.rnc.in_(part))):
                found[r.rnc] = r.status
    return found


def registry_is_fresh(engine: Engine, max_age_days: int = 3) -> bool:
    """Read-only: a missing table simply means 'not loaded yet'."""
    try:
        with engine.connect() as conn:
            r = conn.execute(select(reference_loads.c.loaded_at).where(reference_loads.c.name == "rnc_registry")).first()
    except Exception:
        return False
    return bool(r) and (utcnow() - r.loaded_at) <= timedelta(days=max_age_days)


# ---------------- Banco Central USD rate ----------------

MONTHS = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
          "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12}
DATE_RE = re.compile(r"(\d{1,2})\s+de\s+(" + "|".join(MONTHS) + r")\s+(?:de|del)\s+(\d{4})", re.IGNORECASE)
RATE_RE = re.compile(r"RD\s*\$\s*([0-9]{2,3}[.,][0-9]{1,6})")
MONTH_ALT = "|".join(MONTHS)
RANGE_RE = re.compile(r"del?\s+(\d{1,2})(?:\s+de\s+(" + MONTH_ALT + r"))?(?:\s+(?:de|del)\s+(\d{4}))?\s+al\s+(?:d[ií]a\s+)?"
                      r"(\d{1,2})\s+de\s+(" + MONTH_ALT + r")\s+(?:de|del)\s+(\d{4})", re.IGNORECASE)


def _to_date(m: re.Match) -> date:
    return date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)))


def _range(m: re.Match) -> tuple[date, date]:
    end = date(int(m.group(6)), MONTHS[m.group(5).lower()], int(m.group(4)))
    month = MONTHS[m.group(2).lower()] if m.group(2) else end.month
    year = int(m.group(3)) if m.group(3) else end.year
    start = date(year, month, int(m.group(1)))
    if start > end:                         # "del 31 de diciembre al 2 de enero de 2027"
        start = date(year - 1, month, int(m.group(1)))
    return start, end


def parse_bcrd_notice(text: str, today: date | None = None) -> dict:
    """Reads the daily notice text: buy and sell rate and the dates it covers.
    Returns {"buy": Decimal, "sell": Decimal, "from": date, "to": date}.
    Dates far from today (e.g. a resolution date quoted in the text) are ignored; anything unclear raises."""
    today = today or date.today()
    rates = [Decimal(x.replace(",", ".")) for x in RATE_RE.findall(text)]
    if len(rates) < 2:
        raise ReferenceFormatError("could not find buy and sell rates in the Banco Central notice")
    buy, sell = rates[0], rates[1]
    if not (Decimal(20) < buy < Decimal(200) and Decimal(20) < sell < Decimal(200)) or sell < buy:
        raise ReferenceFormatError(f"rates look wrong: buy {buy}, sell {sell}")
    near = lambda d: abs((d - today).days) <= 10
    m = RANGE_RE.search(text)
    if m:
        start, end = _range(m)
    else:
        dates = [d for d in (_to_date(x) for x in DATE_RE.finditer(text)) if near(d)]
        if not dates:
            raise ReferenceFormatError("no current date in the Banco Central notice")
        start, end = min(dates), max(dates)
    if not (near(start) and near(end)) or not (0 <= (end - start).days <= 7):
        raise ReferenceFormatError(f"unclear validity dates {start} to {end}; load the rate by hand with `cli fx`")
    return {"buy": buy, "sell": sell, "from": start, "to": end}


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)


def save_rate(conn: Connection, notice: dict, rate_type: str = "sell") -> int:
    """Writes the rate for every date the notice covers (it usually spans weekends and holidays).
    rate_type: 'sell' or 'buy' -- each client's accountant decides (client config fx_rate_type).
    A rate entered by hand (source 'manual') is never overwritten."""
    value = notice["sell"] if rate_type == "sell" else notice["buy"]
    d, n = notice["from"], 0
    while d <= notice["to"]:
        row = conn.execute(select(fx_rates.c.source).where(fx_rates.c.rate_date == d)).first()
        if row is None:
            conn.execute(insert(fx_rates).values(rate_date=d, usd_dop=value, source="bcrd"))
            n += 1
        elif row.source != "manual":
            conn.execute(update(fx_rates).where(fx_rates.c.rate_date == d).values(usd_dop=value, source="bcrd"))
            n += 1
        d += timedelta(days=1)
    return n


def save_manual_rate(conn: Connection, day: date, value: Decimal) -> None:
    if conn.execute(select(fx_rates.c.rate_date).where(fx_rates.c.rate_date == day)).first():
        conn.execute(update(fx_rates).where(fx_rates.c.rate_date == day).values(usd_dop=value, source="manual"))
    else:
        conn.execute(insert(fx_rates).values(rate_date=day, usd_dop=value, source="manual"))


def download(url: str, timeout: float = 120) -> bytes:
    # The Banco Central CDN serves yesterday's notice from cache for the plain URL and ignores
    # Cache-Control request headers; only a query string it has not seen yet fetches the current file.
    params = {"t": str(int(time.time()))} if url.startswith("https://cdn.bancentral.gov.do/") else None
    r = httpx.get(url, params=params, timeout=timeout, follow_redirects=True, headers={"User-Agent": "datia-backbone/0.1"})
    r.raise_for_status()
    return r.content
