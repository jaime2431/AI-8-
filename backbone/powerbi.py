"""Power BI REST client for ONE client's workspace, authenticated as that
client's service principal (registered in the client's own tenant).

- trigger_refresh / wait_for_refresh: run the scheduled refresh after the clean tables are ready
- execute_dax: read-only queries, used by the quality check and the portal assistant
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Callable

import httpx

BASE = "https://api.powerbi.com/v1.0/myorg"
SCOPE = "https://analysis.windows.net/powerbi/api/.default"


class PowerBIError(RuntimeError):
    pass


def msal_token_provider(tenant_id: str, client_id: str, client_secret: str) -> Callable[[], str]:
    import msal
    app = msal.ConfidentialClientApplication(
        client_id, authority=f"https://login.microsoftonline.com/{tenant_id}", client_credential=client_secret)

    def get() -> str:
        result = app.acquire_token_for_client(scopes=[SCOPE])
        if "access_token" not in result:
            raise PowerBIError(f"token error: {result.get('error_description', result)}")
        return result["access_token"]
    return get


class PowerBIClient:
    def __init__(self, workspace_id: str, dataset_id: str, token_provider: Callable[[], str],
                 http: httpx.Client | None = None):
        self.workspace_id = workspace_id
        self.dataset_id = dataset_id
        self._token = token_provider
        self._http = http or httpx.Client(timeout=120)

    @property
    def _ds(self) -> str:
        return f"{BASE}/groups/{self.workspace_id}/datasets/{self.dataset_id}"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token()}"}

    def trigger_refresh(self) -> None:
        r = self._http.post(f"{self._ds}/refreshes", headers=self._headers(), json={"notifyOption": "NoNotification"})
        if r.status_code not in (200, 202):
            raise PowerBIError(f"refresh request failed {r.status_code}: {r.text[:300]}")

    def latest_refresh(self) -> dict:
        r = self._http.get(f"{self._ds}/refreshes", headers=self._headers(), params={"$top": 1})
        r.raise_for_status()
        items = r.json().get("value", [])
        return items[0] if items else {}

    def wait_for_refresh(self, timeout_s: int = 3600, poll_s: int = 30, started_after: datetime | None = None) -> dict:
        """Waits for the refresh WE started: entries that began before started_after (UTC) are ignored,
        so an older 'Completed' refresh is never mistaken for tonight's. 'Unknown'/'NotStarted' = still running."""
        deadline = time.monotonic() + timeout_s
        floor = (started_after - timedelta(minutes=2)) if started_after else None
        while True:
            last = self.latest_refresh()
            begun = last.get("startTime")
            is_ours = True
            if floor and begun:
                is_ours = datetime.fromisoformat(begun.replace("Z", "+00:00")).replace(tzinfo=None) >= floor
            if is_ours and last.get("status") in ("Completed", "Failed", "Disabled", "Cancelled"):
                return last
            if time.monotonic() > deadline:
                raise PowerBIError("refresh did not finish in time")
            time.sleep(poll_s)

    def execute_dax(self, query: str, max_rows: int = 1000, impersonated_user: str | None = None) -> list[dict]:
        """Read-only by design: the executeQueries API cannot change data.
        impersonated_user applies the model's row-level security for that user."""
        body: dict = {"queries": [{"query": query}], "serializerSettings": {"includeNulls": True}}
        if impersonated_user:
            body["impersonatedUserName"] = impersonated_user
        r = self._http.post(f"{self._ds}/executeQueries", headers=self._headers(), json=body)
        if r.status_code != 200:
            raise PowerBIError(f"DAX failed {r.status_code}: {r.text[:300]}")
        tables = r.json()["results"][0]["tables"]
        rows = tables[0].get("rows", []) if tables else []
        return rows[:max_rows]
