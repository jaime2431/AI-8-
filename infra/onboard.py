"""One-command cloud setup for a new client.

Run by our AI engineer, signed in with `az login --tenant <client tenant>` (access granted
through GDAP). Every step prints what it does; --dry-run prints the plan without changing anything.

  python infra/onboard.py --client ferrenort --name "Ferretería Norte SRL" --own-rnc 131000002 \
      --tenant <client-tenant-id> --subscription <client-subscription-id> \
      --sql-admin-group-name "Datia SQL Admins" --sql-admin-group-id <group-object-id> [--dry-run]

Environment:
  ANTHROPIC_ADMIN_KEY   our Claude Console Admin API key (sk-ant-admin...), to create the client's workspace
  CLIENT_ANTHROPIC_KEY  optional: the API key created in that workspace (keys can only be created in the Console)

What it does:
  1. resource group + Azure template (database, storage, Key Vault, job identity)
  2. Power BI service principal in the client's tenant
  3. Power BI workspaces "Datia Test - <name>" and "Datia Live - <name>", service principal as Member
  4. Claude Console workspace for the client
  5. secrets into the client's Key Vault
  6. SQL script that gives the job identity access to the clean database
  7. the client's entry in config/clients.json
  and then lists the steps that need a person (approvals and tenant settings).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

import httpx
from urllib.parse import quote_plus

ROOT = Path(__file__).resolve().parent.parent
PBI = "https://api.powerbi.com/v1.0/myorg"
ANTHROPIC = "https://api.anthropic.com/v1/organizations/workspaces"
GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class Runner:
    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        self.log: list[str] = []

    def az(self, *args: str, fake: dict | str | None = None):
        cmd = ["az", *args, "--only-show-errors", "-o", "json"]
        self.log.append(" ".join(shlex.quote(a) for a in cmd))
        print("  $", self.log[-1])
        if self.dry_run:
            return fake if fake is not None else {}
        out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
        return json.loads(out) if out.strip() else {}

    def http(self, method: str, url: str, fake: dict | None = None, ok: tuple = (), **kw):
        self.log.append(f"{method} {url}")
        print(f"  {method} {url}")
        if self.dry_run:
            return fake or {}
        r = httpx.request(method, url, timeout=60, **kw)
        if r.status_code in ok:
            return {"_status": r.status_code}
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {url} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    def retry(self, fn, tries: int = 6, wait_s: int = 20):
        """For Azure role assignments, which take a minute or two to apply."""
        for i in range(tries):
            try:
                return fn()
            except subprocess.CalledProcessError:
                if self.dry_run or i == tries - 1:
                    raise
                print(f"  permission not active yet, retrying in {wait_s}s")
                time.sleep(wait_s)


def sql_url(server: str, database: str, identity_client_id: str) -> str:
    """SQLAlchemy URL for the nightly job: ODBC Driver 18, Entra managed identity, no password anywhere.
    Uses the odbc_connect form so SQLAlchemy does not add Trusted_Connection=Yes (which ODBC 18 rejects
    together with Authentication=)."""
    odbc = (f"Driver={{ODBC Driver 18 for SQL Server}};Server=tcp:{server},1433;Database={database};"
            f"Authentication=ActiveDirectoryMsi;UID={identity_client_id};Encrypt=yes;TrustServerCertificate=no;"
            "Connection Timeout=60")
    return "mssql+pyodbc:///?odbc_connect=" + quote_plus(odbc)


def step(n: int, text: str):
    print(f"\n[{n}] {text}")


def check_tenant(a, clients_file: Path) -> None:
    """A tenant id maps users to a company; a typo or reuse could send one company's users to another's data."""
    if not GUID_RE.match(a.tenant):
        raise SystemExit("--tenant must be the client's Entra tenant id (a GUID)")
    if clients_file.exists():
        for c in json.loads(clients_file.read_text(encoding="utf-8"))["clients"]:
            if c["entra_tenant_id"].lower() == a.tenant.lower() and c["client_id"] != a.client:
                raise SystemExit(f"tenant {a.tenant} already belongs to client {c['client_id']}; refusing to map it twice")


def onboard(a, run: Runner, clients_file: Path) -> dict:
    slug, name = a.client, a.name
    rg = f"rg-datia-{slug}"
    secrets: dict[str, str] = {}
    check_tenant(a, clients_file)

    step(1, "Azure resources (database, storage, Key Vault, job identity)")
    run.az("account", "set", "--subscription", a.subscription)
    me = run.az("ad", "signed-in-user", "show", fake={"id": "44444444-0000-0000-0000-00000000de91"})
    run.az("group", "create", "-n", rg, "-l", a.location, "--tags", f"client={slug}", "managedBy=datia")
    dep = run.az("deployment", "group", "create", "-g", rg, "-f", str(ROOT / "infra" / "client.bicep"),
                 "-p", f"clientSlug={slug}", f"sqlAdminGroupName={a.sql_admin_group_name}",
                 f"sqlAdminGroupObjectId={a.sql_admin_group_id}", f"deployerObjectId={me['id']}",
                 fake={"properties": {"outputs": {
                     "sqlServer": {"value": f"sql-datia-{slug}-x.database.windows.net"}, "sqlDatabase": {"value": "clean"},
                     "keyVaultName": {"value": f"kv-datia-{slug}-x"}, "keyVaultUri": {"value": f"https://kv-datia-{slug}-x.vault.azure.net/"},
                     "storageAccount": {"value": f"stdatia{slug}x"}, "jobIdentityName": {"value": f"id-datia-{slug}"},
                     "jobIdentityClientId": {"value": "00000000-0000-0000-0000-00000000c1d0"}}}})
    out = {k: v["value"] for k, v in dep["properties"]["outputs"].items()}
    secrets["database-url"] = sql_url(out["sqlServer"], out["sqlDatabase"], out["jobIdentityClientId"])

    step(2, "Power BI service principal in the client's tenant (reused if it already exists)")
    app_name = f"Datia Power BI - {name}"
    found = run.az("ad", "app", "list", "--display-name", app_name, fake=[])
    if found:
        app = found[0]
        print(f"  reusing app {app['appId']}")
        sp = run.az("ad", "sp", "show", "--id", app["appId"], fake={"id": "22222222-aaaa-bbbb-cccc-000000000002"})
    else:
        app = run.az("ad", "app", "create", "--display-name", app_name, "--sign-in-audience", "AzureADMyOrg",
                     fake={"appId": "11111111-aaaa-bbbb-cccc-000000000001"})
        sp = run.az("ad", "sp", "create", "--id", app["appId"], fake={"id": "22222222-aaaa-bbbb-cccc-000000000002"})
    secret_expiry = None
    if not found or a.rotate_secret:
        cred = run.az("ad", "app", "credential", "reset", "--id", app["appId"], "--years", "1", "--append",
                      "--display-name", "datia-pipeline", fake={"password": "dry-run-secret"})
        secrets["powerbi-client-secret"] = cred["password"]
        secret_expiry = (date.today() + timedelta(days=365)).isoformat()
        print(f"  new client secret, expires {secret_expiry}: put a renewal reminder 30 days before")
    secrets["powerbi-client-id"] = app["appId"]

    step(3, "Power BI workspaces (Test and Live) with the service principal as Member")
    tok = run.az("account", "get-access-token", "--resource", "https://analysis.windows.net/powerbi/api",
                 fake={"accessToken": "dry-run"})["accessToken"]
    hdr = {"Authorization": f"Bearer {tok}"}
    workspaces = {}
    for kind in ("Test", "Live"):
        ws_name = f"Datia {kind} - {name}"
        existing = run.http("GET", f"{PBI}/groups", headers=hdr, params={"$filter": f"name eq '{ws_name}'"},
                            fake={"value": []}).get("value", [])
        if existing:
            ws = existing[0]
            print(f"  reusing workspace {ws['id']}")
        else:
            ws = run.http("POST", f"{PBI}/groups?workspaceV2=True", headers=hdr, json={"name": ws_name},
                          fake={"id": f"33333333-0000-0000-0000-00000000{'7e57' if kind == 'Test' else '11fe'}"})
        workspaces[kind] = ws["id"]
        added = run.http("POST", f"{PBI}/groups/{ws['id']}/users", headers=hdr, ok=(400, 401, 403),
                         json={"identifier": sp["id"], "principalType": "App", "groupUserAccessRight": "Member"})
        if added.get("_status") in (401, 403):
            raise SystemExit("Power BI refused the service principal. In the client's Power BI admin portal, allow "
                             "service principals to use Power BI APIs for a security group containing "
                             f"'{app_name}', wait 15 minutes, then run this script again (it reuses what exists).")
        if added.get("_status") == 400:
            print("  service principal already in the workspace (or request rejected); check workspace access")

    step(4, "Claude Console workspace for this client")
    admin_key = os.environ.get("ANTHROPIC_ADMIN_KEY", "")
    if admin_key or run.dry_run:
        cw = run.http("POST", ANTHROPIC, headers={"x-api-key": admin_key, "anthropic-version": "2023-06-01"},
                      json={"name": f"datia-{slug}"}, fake={"id": "wrkspc_dryrun"})
        print(f"  Claude workspace: {cw.get('id')}")
    else:
        print("  ANTHROPIC_ADMIN_KEY not set: create the workspace 'datia-%s' in the Claude Console by hand." % slug)
    if os.environ.get("CLIENT_ANTHROPIC_KEY"):
        secrets["anthropic-api-key"] = os.environ["CLIENT_ANTHROPIC_KEY"]

    step(5, "Secrets into the client's Key Vault (passed through a private temp file, never on the command line)")
    for k, v in secrets.items():
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".secret") as fh:
            fh.write(v)
            path = fh.name
        os.chmod(path, 0o600)
        try:
            run.retry(lambda: run.az("keyvault", "secret", "set", "--vault-name", out["keyVaultName"], "-n", k,
                                 "--file", path, "--encoding", "utf-8"))
        finally:
            os.remove(path)

    step(6, "SQL access for the job identity")
    sql_file = ROOT / "infra" / "generated" / f"{slug}-grant.sql"
    sql_file.parent.mkdir(parents=True, exist_ok=True)
    ident = out["jobIdentityName"]
    sql_file.write_text(
        f"-- Run in the 'clean' database of {out['sqlServer']} (Azure portal > Query editor, signed in as a member of the SQL admin group)\n"
        f"CREATE USER [{ident}] FROM EXTERNAL PROVIDER;\n"
        f"ALTER ROLE db_datareader ADD MEMBER [{ident}];\n"
        f"ALTER ROLE db_datawriter ADD MEMBER [{ident}];\n"
        f"ALTER ROLE db_ddladmin ADD MEMBER [{ident}];\n", encoding="utf-8")
    print(f"  wrote {sql_file.relative_to(ROOT)}")

    step(7, f"Client entry in {clients_file.relative_to(ROOT) if clients_file.is_relative_to(ROOT) else clients_file}")
    entry = {
        "client_id": slug, "name": name, "own_rnc": a.own_rnc, "entra_tenant_id": a.tenant.lower(),
        "key_vault_url": out["keyVaultUri"], "managed_identity_client_id": out["jobIdentityClientId"],
        "inbox_path": f"/data/{slug}/inbox",
        "connectors": [{"type": "ecf_folder", "name": "ecf", "path": f"/data/{slug}/ecf"}],
        "powerbi": {"tenant_id": a.tenant.lower(), "workspace_id": workspaces["Live"], "dataset_id": "SET-AFTER-PUBLISHING",
                    "report_id": None},
        "qa_checks": [], "summary_recipients": [], "confidence_threshold": 0.9,
        "require_known_suppliers": False, "model_description": "",
    }
    data = json.loads(clients_file.read_text(encoding="utf-8")) if clients_file.exists() else {"clients": []}
    data["clients"] = [c for c in data["clients"] if c["client_id"] != slug] + [entry]
    clients_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    manual = [
        f"Client admin approves our GDAP request (roles: Power BI Administrator, User Administrator, Global Reader).",
        *( [f"Calendar reminder: renew the Power BI client secret before {secret_expiry} "
            f"(python infra/onboard.py ... --rotate-secret)."] if secret_expiry else [] ),
        f"Run {sql_file.name} in the clean database (step 6).",
        "Create the API key in the client's Claude Console workspace, set its monthly spend limit, then: "
        f"az keyvault secret set --vault-name {out['keyVaultName']} -n anthropic-api-key --value <key>"
        if "anthropic-api-key" not in secrets else "Set the monthly spend limit on the client's Claude workspace.",
        "Power BI admin portal (client tenant): allow service principals to use Power BI APIs, the executeQueries "
        "API and the MCP server endpoint, for a security group that contains the Datia service principal.",
        "Publish the semantic model and report to the Test workspace, check numbers, then to Live; put dataset_id and "
        "report_id in clients.json and add the QA checks with the client's DAX measures.",
        "Assign the client's users to the portal app; give the Reviewer role to the people who approve invoices.",
        f"python -m backbone.cli init-db --client {slug}  (signed in as SQL admin)",
        f"Run the nightly job with the identity {out['jobIdentityName']} (Container Apps job: user-assigned identity).",
        "Run the isolation tests and one nightly run in Test before go-live.",
    ]
    print("\nSteps that need a person:")
    for i, m in enumerate(manual, 1):
        print(f"  {i}. {m}")
    return {"outputs": out, "workspaces": workspaces, "database_url": secrets["database-url"], "secret_expiry": secret_expiry, "secrets_set": sorted(secrets), "manual": manual, "commands": run.log}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--client", required=True, help="slug, lowercase letters and numbers, max 9 characters")
    p.add_argument("--name", required=True)
    p.add_argument("--own-rnc", required=True)
    p.add_argument("--tenant", required=True, help="client's Microsoft Entra tenant id")
    p.add_argument("--subscription", required=True, help="client's Azure subscription (bought through CSP)")
    p.add_argument("--location", default="eastus2")
    p.add_argument("--sql-admin-group-name", required=True)
    p.add_argument("--sql-admin-group-id", required=True)
    p.add_argument("--clients-file", default=str(ROOT / "config" / "clients.json"))
    p.add_argument("--rotate-secret", action="store_true", help="issue a new Power BI client secret even if the app exists")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    if not re.fullmatch(r"[a-z0-9]{3,9}", a.client):
        p.error("--client must be 3-9 lowercase letters or numbers (Azure name limits)")
    result = onboard(a, Runner(a.dry_run), Path(a.clients_file))
    print("\nDone." + (" (dry run: nothing was changed in Azure, Power BI or Claude)" if a.dry_run else ""))
    return result


if __name__ == "__main__":
    main(sys.argv[1:])
