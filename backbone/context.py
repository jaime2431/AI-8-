"""ClientContext: the only way code gets to a client's resources.

One context = one client. It builds that client's database engine, Claude client
and Power BI client from that client's own secrets. Code never mixes contexts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property

from . import settings
from .db import make_engine
from .powerbi import PowerBIClient, msal_token_provider
from .tenancy import ClientConfig, ClientRegistry, SecretStore


@dataclass
class ClientContext:
    config: ClientConfig
    secrets: SecretStore = field(init=False)

    def __post_init__(self):
        self.secrets = SecretStore(self.config)

    @property
    def client_id(self) -> str:
        return self.config.client_id

    @cached_property
    def engine(self):
        return make_engine(self.secrets.get(self.config.database_url_secret))

    @cached_property
    def claude(self):
        import anthropic
        return anthropic.Anthropic(api_key=self.secrets.get(self.config.anthropic_key_secret))

    @cached_property
    def powerbi(self) -> PowerBIClient | None:
        p = self.config.powerbi
        if p is None or p.dataset_id.startswith("SET-"):      # not published yet (set by onboarding)
            return None
        tokens = msal_token_provider(p.tenant_id, self.secrets.get(p.client_id_secret),
                                     self.secrets.get(p.client_secret_secret))
        return PowerBIClient(p.workspace_id, p.dataset_id, tokens)

    @cached_property
    def reference_engine(self):
        """Shared PUBLIC reference data (DGII RNC registry); None when not configured."""
        return make_engine(settings.REFERENCE_DB_URL) if settings.REFERENCE_DB_URL else None

    model_agent: str = settings.MODEL_AGENT
    model_fast: str = settings.MODEL_FAST


def open_client(registry: ClientRegistry, client_id: str) -> ClientContext:
    return ClientContext(registry.get(client_id))
