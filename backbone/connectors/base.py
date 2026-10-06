from __future__ import annotations

import csv
import shutil
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Protocol

from ..ecf import ECFError, NotAnInvoice, parse_ecf
from ..validation import normalize_ncf

MONEY_FIELDS = {"subtotal", "taxable_amount", "itbis", "other_taxes", "tip", "total"}
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y")


class Connector(Protocol):
    name: str

    def extract(self, since: datetime | None) -> Iterable[dict]:
        ...


def parse_date(v) -> date | None:
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(str(v).strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_money(v) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v).replace("RD$", "").replace("$", "").replace(",", "").strip())
    except InvalidOperation:
        return None


def apply_mapping(raw: dict, mapping: dict, direction: str | None, source_ref: str) -> dict:
    row = {field: raw.get(col) for col, field in mapping.items()}
    for f in MONEY_FIELDS & row.keys():
        row[f] = parse_money(row[f])
    if "issue_date" in row:
        row["issue_date"] = parse_date(row["issue_date"])
    row.setdefault("currency", "DOP")
    row["currency"] = (row["currency"] or "DOP").upper()
    if direction:
        row["direction"] = direction
    if "ncf" in row:
        row["ncf"] = normalize_ncf(row["ncf"])
    row["source_ref"] = source_ref
    row.setdefault("confidence", {})
    return row


class _FolderSource:
    """Files are moved to processed/ only when commit() is called, i.e. after the rows are saved."""

    def __init__(self, name: str, path: str):
        self.name, self.path = name, Path(path)
        self._done: list[Path] = []

    def commit(self):
        (self.path / "processed").mkdir(parents=True, exist_ok=True)
        for f in self._done:
            if f.exists():
                shutil.move(str(f), self.path / "processed" / f.name)
        self._done = []


class ECFFolder(_FolderSource):
    """Reads e-CF XML files dropped in a folder (synced from the client's e-CF provider or DGII).
    Unreadable files move to failed/ straight away and are logged."""

    def __init__(self, name: str, path: str, own_rnc: str):
        super().__init__(name, path)
        self.own_rnc = own_rnc

    def extract(self, since=None):
        for sub in ("failed", "skipped"):
            (self.path / sub).mkdir(parents=True, exist_ok=True)
        for f in sorted(self.path.glob("*.xml")):
            try:
                row = parse_ecf(f.read_bytes(), self.own_rnc)
            except NotAnInvoice:
                shutil.move(str(f), self.path / "skipped" / f.name)   # acknowledgments etc.: not invoices
                continue
            except (ECFError, ValueError, InvalidOperation) as e:
                shutil.move(str(f), self.path / "failed" / f.name)
                yield {"_error": f"{f.name}: {e}"}
                continue
            row["source_ref"] = f.name
            self._done.append(f)
            yield row


class CSVFolder(_FolderSource):
    """Reads CSV exports (point of sale, Excel saved as CSV, Google Sheets export) from a folder."""

    def __init__(self, name: str, path: str, mapping: dict, direction: str | None = None, delimiter: str = ","):
        super().__init__(name, path)
        self.mapping, self.direction, self.delimiter = mapping, direction, delimiter

    def extract(self, since=None):
        for f in sorted(self.path.glob("*.csv")):
            with f.open(newline="", encoding="utf-8-sig") as fh:
                for i, raw in enumerate(csv.DictReader(fh, delimiter=self.delimiter), start=2):
                    yield apply_mapping(raw, self.mapping, self.direction, f"{f.name}:{i}")
            self._done.append(f)


class SQLSource:
    """Read-only incremental copy from a client's ERP database (through the data gateway host or a VPN).
    The query must take :since and return only rows changed after it (the pipeline stores the watermark).
    key_column gives each row a stable reference. Use a READ-ONLY database user."""

    def __init__(self, name: str, url: str, query: str, mapping: dict, direction: str | None = None,
                 key_column: str | None = None, clock_query: str = "SELECT CURRENT_TIMESTAMP"):
        self.name, self.url, self.query, self.mapping, self.direction = name, url, query, mapping, direction
        self.key_column, self.clock_query = key_column, clock_query
        self._clock: datetime | None = None

    def source_clock(self) -> datetime | None:
        """The ERP's own 'now', read just before extracting: the watermark must be on the ERP's clock
        (local time), never on our UTC clock, or rows changed in the gap are skipped forever."""
        return self._clock

    def extract(self, since=None):
        from sqlalchemy import create_engine, text
        eng = create_engine(self.url, future=True)
        with eng.connect() as conn:
            clock = conn.execute(text(self.clock_query)).scalar()
            self._clock = clock if isinstance(clock, datetime) else datetime.fromisoformat(str(clock))
            result = conn.execute(text(self.query), {"since": since or datetime(2000, 1, 1)}).mappings()
            for i, raw in enumerate(result):
                ref = raw.get(self.key_column) if self.key_column else i
                yield apply_mapping(dict(raw), self.mapping, self.direction, f"{self.name}:{ref}")


def build_connector(spec: dict, ctx) -> Connector:
    kind = spec["type"]
    name = spec.get("name", kind)
    if kind == "ecf_folder":
        return ECFFolder(name, spec["path"], ctx.config.own_rnc)
    if kind == "csv_folder":
        return CSVFolder(name, spec["path"], spec["mapping"], spec.get("direction"), spec.get("delimiter", ","))
    if kind == "sql":
        return SQLSource(name, ctx.secrets.get(spec["url_secret"]), spec["query"], spec["mapping"], spec.get("direction"),
                         spec.get("key_column"), spec.get("clock_query", "SELECT CURRENT_TIMESTAMP"))
    raise ValueError(f"unknown connector type {kind}")
