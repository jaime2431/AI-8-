"""Shared test setup: two clients (alpha, beta), each with its own SQLite database,
plus fake Claude and fake Power BI so tests never call real services."""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from backbone.context import ClientContext
from backbone.db import create_tables
from backbone.tenancy import ClientRegistry

FIXTURES = Path(__file__).parent / "fixtures"
OWN_RNC = "131000002"


class FakePowerBI:
    def __init__(self, values=None, refresh_status="Completed"):
        self.values = values or {}          # dax substring -> value
        self.refresh_status = refresh_status
        self.queries: list[str] = []
        self.refreshes = 0

    def trigger_refresh(self):
        self.refreshes += 1

    def wait_for_refresh(self, timeout_s=0, poll_s=0, started_after=None):
        return {"status": self.refresh_status}

    def execute_dax(self, query, max_rows=1000, impersonated_user=None):
        self.queries.append(query)
        for key, val in self.values.items():
            if key in query:
                return [{"[v]": val}]
        return [{"[v]": 0}]


@dataclass
class Block:
    type: str
    text: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)
    id: str = "tu_1"


class FakeClaude:
    """Returns scripted responses in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=self.responses.pop(0))


def make_config(tmp: Path, cid: str, tenant: str) -> dict:
    base = tmp / cid
    for sub in ("ecf", "pos", "inbox"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    return {
        "client_id": cid, "name": f"Empresa {cid}", "own_rnc": OWN_RNC, "entra_tenant_id": tenant,
        "inbox_path": str(base / "inbox"),
        "connectors": [
            {"type": "ecf_folder", "name": "ecf", "path": str(base / "ecf")},
            {"type": "csv_folder", "name": "pos", "path": str(base / "pos"), "direction": "sale",
             "mapping": {"Fecha": "issue_date", "NCF": "ncf", "RNC": "issuer_rnc", "Subtotal": "subtotal",
                         "ITBIS": "itbis", "Total": "total"}},
        ],
        "qa_checks": [
            {"name": "ventas", "db_metric": "sales_total_dop", "dax": "VENTAS {date}"},
            {"name": "compras", "db_metric": "purchases_total_dop", "dax": "COMPRAS {date}"},
        ],
        "summary_recipients": ["owner@example.do"],
        "open_period_start": "2026-01-01",
    }


@pytest.fixture
def registry(tmp_path, monkeypatch):
    data = {"clients": [make_config(tmp_path, "alpha", "tid-alpha"), make_config(tmp_path, "beta", "tid-beta")]}
    path = tmp_path / "clients.json"
    path.write_text(json.dumps(data))
    for cid in ("alpha", "beta"):
        monkeypatch.setenv(f"DATIA__{cid.upper()}__DATABASE_URL", f"sqlite:///{tmp_path / cid / 'clean.db'}")
        monkeypatch.setenv(f"DATIA__{cid.upper()}__ANTHROPIC_API_KEY", f"test-key-{cid}")
    reg = ClientRegistry.from_file(path)
    reg.tmp = tmp_path
    return reg


def make_ctx(registry, cid, powerbi=None, claude=None) -> ClientContext:
    ctx = ClientContext(registry.get(cid))
    create_tables(ctx.engine)
    ctx.__dict__["powerbi"] = powerbi        # override the lazy builders with fakes
    ctx.__dict__["claude"] = claude
    return ctx


def drop_ecf(registry, cid, *names):
    for n in names:
        shutil.copy(FIXTURES / n, registry.tmp / cid / "ecf" / n)


def drop_csv(registry, cid, name, text):
    (registry.tmp / cid / "pos" / name).write_text(text, encoding="utf-8")
