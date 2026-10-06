"""Client (tenant) configuration and secrets.

Isolation rule: everything that touches a client's data is built from that
client's own configuration and that client's own secrets. Nothing in this
module returns a resource that is shared between two clients.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path


class UnknownClient(KeyError):
    pass


class MissingSecret(KeyError):
    pass


@dataclass(frozen=True)
class PowerBIConfig:
    tenant_id: str          # the client's own Microsoft tenant
    workspace_id: str       # the client's Live workspace
    dataset_id: str         # the client's semantic model
    client_id_secret: str = "powerbi-client-id"        # secret names in the client's vault
    client_secret_secret: str = "powerbi-client-secret"
    report_id: str | None = None        # the report shown on the portal's home screen


@dataclass(frozen=True)
class QACheck:
    """Compares one number in the clean database with the same number in Power BI."""
    name: str
    db_metric: str          # see agents.quality_check.DB_METRICS
    dax: str                # DAX query; "{date}" is replaced by DATE(y, m, d)
    tolerance_pct: float = 0.5


@dataclass(frozen=True)
class ClientConfig:
    client_id: str                      # short slug, e.g. "ferreteria-norte"
    name: str
    own_rnc: str                        # the client's RNC, used to tell sales from purchases
    entra_tenant_id: str                # maps portal logins to this client
    key_vault_url: str | None = None    # the client's own Key Vault
    managed_identity_client_id: str | None = None   # the nightly job's user-assigned identity (set by onboarding)
    database_url_secret: str = "database-url"
    anthropic_key_secret: str = "anthropic-api-key"   # key from the client's own Claude Console workspace
    inbox_path: str | None = None       # where incoming documents are stored
    connectors: tuple[dict, ...] = ()
    powerbi: PowerBIConfig | None = None
    qa_checks: tuple[QACheck, ...] = ()
    summary_recipients: tuple[str, ...] = ()
    alert_webhook_secret: str | None = None      # our team's channel for this client (Teams/Slack webhook URL)
    smtp_url_secret: str | None = None           # 'smtp-url' in the vault: sends the owner's morning summary
    confidence_threshold: float = 0.90
    open_period_start: date | None = None
    require_known_suppliers: bool = False
    model_description: str = ""         # plain description of Power BI tables and measures for the assistant
    fx_rate_type: str = "sell"          # Banco Central rate the client's books use: sell | buy
    rls_enabled: bool = False           # if True, the portal refuses questions from users without a UPN


CLIENT_ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _parse_client(raw: dict) -> ClientConfig:
    raw = dict(raw)
    pbi = raw.pop("powerbi", None)
    checks = raw.pop("qa_checks", [])
    ops = raw.pop("open_period_start", None)
    if not CLIENT_ID_RE.match(raw.get("client_id", "")):
        raise ValueError(f"client_id {raw.get('client_id')!r} must be lowercase letters, numbers and dashes")
    raw["entra_tenant_id"] = raw["entra_tenant_id"].lower()
    return ClientConfig(
        powerbi=PowerBIConfig(**pbi) if pbi else None,
        qa_checks=tuple(QACheck(**c) for c in checks),
        open_period_start=date.fromisoformat(ops) if ops else None,
        connectors=tuple(raw.pop("connectors", [])),
        summary_recipients=tuple(raw.pop("summary_recipients", [])),
        **raw,
    )


class ClientRegistry:
    def __init__(self, clients: dict[str, ClientConfig]):
        self._clients = dict(clients)
        env_names = [cid.upper().replace("-", "_") for cid in clients]
        if len(env_names) != len(set(env_names)):
            raise ValueError("Two client ids collide; every client needs a distinct id.")
        tenants = [c.entra_tenant_id.lower() for c in clients.values()]
        if len(tenants) != len(set(tenants)):
            raise ValueError("Two clients share an Entra tenant id; every client needs its own tenant.")

    @classmethod
    def from_file(cls, path: str | Path) -> "ClientRegistry":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        clients = {c["client_id"]: _parse_client(c) for c in data["clients"]}
        return cls(clients)

    def get(self, client_id: str) -> ClientConfig:
        try:
            return self._clients[client_id]
        except KeyError:
            raise UnknownClient(client_id) from None

    def by_entra_tenant(self, tenant_id: str) -> ClientConfig:
        tenant_id = (tenant_id or "").lower()
        for c in self._clients.values():
            if c.entra_tenant_id.lower() == tenant_id:
                return c
        raise UnknownClient(tenant_id)

    def ids(self) -> list[str]:
        return sorted(self._clients)


class SecretStore:
    """Reads one client's secrets.

    Production: that client's own Azure Key Vault (key_vault_url).
    Development: environment variables named DATIA__<CLIENT>__<SECRET>,
    e.g. DATIA__FERRETERIA_NORTE__DATABASE_URL.
    """

    def __init__(self, client: ClientConfig):
        self.client = client
        self._vault = None

    def _env_name(self, name: str) -> str:
        norm = lambda s: s.upper().replace("-", "_")
        return f"DATIA__{norm(self.client.client_id)}__{norm(name)}"

    def get(self, name: str) -> str:
        from . import settings
        env = self._env_name(name)
        # Environment variables are for development, or for clients with no vault yet.
        # A client with a Key Vault in production always reads from its own vault.
        if env in os.environ and (settings.ENV == "dev" or not self.client.key_vault_url):
            return os.environ[env]
        if self.client.key_vault_url:
            if self._vault is None:
                from azure.identity import DefaultAzureCredential, ManagedIdentityCredential  # optional dependency
                from azure.keyvault.secrets import SecretClient
                mi = self.client.managed_identity_client_id
                cred = ManagedIdentityCredential(client_id=mi) if mi and os.environ.get("DATIA_ENV") != "dev" \
                    else DefaultAzureCredential(managed_identity_client_id=mi)
                self._vault = SecretClient(self.client.key_vault_url, cred)
            return self._vault.get_secret(name).value
        raise MissingSecret(f"{name} for client {self.client.client_id} (set {env} or key_vault_url)")
