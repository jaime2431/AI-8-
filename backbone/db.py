r"""The clean database. Each client has its OWN database (separate Azure SQL database);
these tables are created inside it. There is no client_id column because no table
ever holds two clients' data.

Flow: documents / connectors -> staging_invoices -> (validation) -> clean_invoices
                                                              \-> review_queue
Power BI reads ONLY clean_invoices (and master lists).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import (Column, Date, DateTime, ForeignKey, Integer, MetaData, Numeric, String, Table, Text,
                        UniqueConstraint, create_engine, insert)
from sqlalchemy.engine import Connection, Engine

metadata = MetaData()

MONEY = Numeric(18, 2)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def business_today():
    """Today's date in the company's time zone (not the server's UTC clock)."""
    from . import settings
    return datetime.now(settings.TZ).date()


documents = Table(
    "documents", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("received_at", DateTime, nullable=False, default=utcnow),
    Column("channel", String(20), nullable=False),          # email | whatsapp | upload | folder
    Column("filename", String(255)),
    Column("storage_path", String(500), nullable=False),
    Column("media_type", String(100), nullable=False),
    Column("sha256", String(64), nullable=False, unique=True),
    Column("status", String(20), nullable=False, default="pending"),   # pending | retry | extracted | failed
    Column("error", Text),
)

_invoice_columns = lambda: [
    Column("direction", String(10)),        # sale | purchase
    Column("issuer_rnc", String(11)),
    Column("issuer_name", String(255)),
    Column("buyer_rnc", String(11)),
    Column("ncf", String(13)),
    Column("ncf_modified", String(13)),     # for credit notes: the invoice they correct
    Column("issue_date", Date),
    Column("currency", String(3)),
    Column("subtotal", MONEY),
    Column("taxable_amount", MONEY),
    Column("itbis", MONEY),
    Column("expected_itbis", MONEY),
    Column("other_taxes", MONEY),
    Column("tip", MONEY),
    Column("total", MONEY),
    Column("branch", String(100)),
]

staging_invoices = Table(
    "staging_invoices", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", DateTime, nullable=False, default=utcnow),
    Column("source", String(50), nullable=False),            # connector name or "document"
    Column("source_ref", String(500)),                       # file name, row key, etc.
    Column("document_id", Integer, ForeignKey("documents.id")),
    *_invoice_columns(),
    Column("lines_json", Text),
    Column("confidence_json", Text),
    Column("status", String(10), nullable=False, default="new"),     # new | accepted | review | rejected | error
    Column("rules_json", Text),
)

clean_invoices = Table(
    "clean_invoices", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("staging_id", Integer, ForeignKey("staging_invoices.id"), unique=True, nullable=False),
    *_invoice_columns(),
    Column("ncf_type", String(3)),
    Column("fx_rate", Numeric(10, 4)),
    Column("total_dop", MONEY),
    Column("source", String(50), nullable=False),
    Column("source_ref", String(500)),
    Column("document_id", Integer),
    Column("approved_by", String(100), nullable=False),      # "rules" or "user:<id>"
    Column("loaded_at", DateTime, nullable=False, default=utcnow),
    UniqueConstraint("issuer_rnc", "ncf", name="uq_clean_issuer_ncf"),
)

review_queue = Table(
    "review_queue", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("staging_id", Integer, ForeignKey("staging_invoices.id"), nullable=False),
    Column("reason", Text, nullable=False),
    Column("created_at", DateTime, nullable=False, default=utcnow),
    Column("status", String(10), nullable=False, default="open"),    # open | approved | rejected
    Column("resolved_by", String(100)),
    Column("resolved_at", DateTime),
    Column("corrections_json", Text),
)

master_suppliers = Table(
    "master_suppliers", metadata,
    Column("rnc", String(11), primary_key=True),
    Column("name", String(255)),
    Column("first_seen", DateTime, default=utcnow),
)

connector_state = Table(
    "connector_state", metadata,
    Column("name", String(50), primary_key=True),
    Column("last_run", DateTime, nullable=False),            # watermark passed as :since on the next run
)

fx_rates = Table(
    "fx_rates", metadata,
    Column("rate_date", Date, primary_key=True),
    Column("usd_dop", Numeric(10, 4), nullable=False),        # Banco Central rate for the day
    Column("source", String(10), nullable=False, default="bcrd"),   # bcrd | manual (manual is never overwritten)
)

pipeline_runs = Table(
    "pipeline_runs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_date", Date, nullable=False),
    Column("started_at", DateTime, nullable=False, default=utcnow),
    Column("finished_at", DateTime),
    Column("status", String(20), nullable=False, default="running"),   # running | passed | held | failed
    Column("stats_json", Text),
    Column("qa_json", Text),
    Column("summary", Text),
)

assistant_messages = Table(
    "assistant_messages", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", String(100), nullable=False),
    Column("created_at", DateTime, nullable=False, default=utcnow),
    Column("role", String(10), nullable=False),               # user | assistant
    Column("content", Text, nullable=False),
)

audit_log = Table(
    "audit_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", DateTime, nullable=False, default=utcnow),
    Column("actor", String(100), nullable=False),
    Column("action", String(50), nullable=False),
    Column("detail", Text),
)


def make_engine(url: str) -> Engine:
    return create_engine(url, future=True, pool_pre_ping=True)


def create_tables(engine: Engine) -> None:
    metadata.create_all(engine)


def audit(conn: Connection, actor: str, action: str, detail: dict | str | None = None) -> None:
    if isinstance(detail, dict):
        detail = json.dumps(detail, default=str, ensure_ascii=False)
    conn.execute(insert(audit_log).values(actor=actor, action=action, detail=detail))
