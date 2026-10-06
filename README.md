# Datia backbone

Codebase for the data and AI service: one clean database per client, validation rules,
Claude agents, Power BI refresh and quality checks, daily DGII and Banco Central reference
data, the client portal (API and screens) and a one-command client cloud setup.

Status: **version 0.3**. All logic runs and is tested offline (63 tests, including client-isolation
and security regression tests from three independent reviews) with fake Claude and fake Power BI.
Before the first real client, finish the open items at the end.

## How data flows (per client)

```
client systems ──► connectors ──► staging_invoices ──► validation rules ──► clean_invoices ──► Power BI refresh
documents ──► Claude invoice reader ──┘                        └──► review_queue (people approve/correct)
after refresh: quality check (Power BI vs clean DB, volume) ──► morning summary or alert
portal: Claude assistant answers questions with READ-ONLY DAX on that client's model
```

Claude never writes into Power BI. Power BI reads only `clean_invoices`.

## Folder map

| Path | What it does |
| --- | --- |
| `backbone/tenancy.py` | Client registry and secrets. Each client has its own Key Vault, database, Claude key, Power BI workspace |
| `backbone/context.py` | `ClientContext`: the only way code reaches a client's resources (one context = one client) |
| `backbone/db.py` | Tables inside each client's clean database |
| `backbone/validation.py` | The 10 data-quality rules (RNC, NCF/e-NCF, totals, ITBIS, duplicates, dates, currency, suppliers, confidence) |
| `backbone/ecf.py` | Reads DGII e-CF XML invoices |
| `backbone/connectors/` | e-CF folder, CSV folder (POS, Excel as CSV), read-only SQL (local ERPs) |
| `backbone/agents/invoice_reader.py` | Claude reads PDF/photo invoices into fields with confidence scores |
| `backbone/agents/quality_check.py` | Compares Power BI with the clean DB, volume check, Spanish summary |
| `backbone/agents/assistant.py` | Portal assistant: Claude + read-only DAX tool |
| `backbone/powerbi.py` | Power BI REST: refresh, wait, executeQueries (service principal) |
| `backbone/pipeline.py` | Nightly run, intake, review approvals |
| `backbone/notify.py` | Owner summaries by email (SMTP), team alerts to Teams/Slack, job log as the last resort |
| `backbone/cli.py` | Commands for operators and the scheduler |
| `backbone/reference.py` | Daily DGII RNC registry and Banco Central USD rate loaders |
| `portal/app.py` | FastAPI portal API and page; company comes only from the signed-in user's Entra tenant |
| `portal/web/portal.html` | Portal screens: home (report + last night's check), review queue, documents, assistant |
| `infra/client.bicep` | Per-client Azure resources (SQL serverless, storage, Key Vault, job identity) |
| `infra/onboard.py` | One-command client cloud setup (Azure, Power BI, Claude workspace, secrets); `--dry-run` shows the plan |
| `config/clients.example.json` | Example client configuration |
| `tests/` | Validation, pipeline, client-isolation and hardening tests |

## Run it locally

```bash
pip install -e ".[dev]"
pytest -q                                   # 63 tests, no network needed

# try one client with SQLite
cp config/clients.example.json config/clients.json   # edit paths to local folders
export DATIA__FERRETERIA_NORTE__DATABASE_URL=sqlite:///./data/ferreteria-norte.db
export DATIA__FERRETERIA_NORTE__ANTHROPIC_API_KEY=sk-ant-...   # key from that client's Claude Console workspace
python -m backbone.cli init-db --client ferreteria-norte
python -m backbone.cli ingest  --client ferreteria-norte --file factura.pdf
python -m backbone.cli nightly --client ferreteria-norte --date 2026-10-02
python -m backbone.cli review  --client ferreteria-norte

# check real e-CF files without a database
python -m backbone.cli check-ecf ~/Downloads/*.xml --own-rnc 131000002

# portal (development login): open http://localhost:8000/?client=ferreteria-norte
DATIA_ENV=dev uvicorn portal.app:app --reload
```

## Daily schedule (Dominican Republic time)

| Time | Command | What it does |
| --- | --- | --- |
| 23:00 | `python -m backbone.cli refresh-fx` | Banco Central USD rate into every client database (each client's own buy/sell choice; manual rates kept) |
| 23:15 | `python -m backbone.cli refresh-rnc` | DGII RNC registry into the shared reference database (public data only) |
| 00:30 | `python -m backbone.cli nightly-all` | Each client's pipeline, one at a time, each in its own context |

Run them as Azure Container Apps jobs with the client's user-assigned identity. All dates use the Dominican
Republic clock (`DATIA_TZ`), not the container's UTC. A night exits non-zero when any client's run is not
`passed`, so the scheduler can alert. A second run for a client while one is in progress is refused (run lock);
a run older than 6 hours still marked running is treated as abandoned. Each client's night reads at most 500
documents (the rest wait) and a document that failed for a transient reason gets exactly one more try. If the Banco Central notice cannot be read with certainty, `refresh-fx`
stops and asks for `cli fx --client <id> --date <d> --rate <r>`; if the registry is older than 3 days,
validation runs without it rather than failing.

## Production setup per client

```bash
az login --tenant <client-tenant-id>          # our admin account, access through GDAP
export ANTHROPIC_ADMIN_KEY=sk-ant-admin-...   # creates the client's Claude workspace
python infra/onboard.py --client ferrenort --name "Ferretería Norte SRL" --own-rnc 131000002 \
    --tenant <client-tenant-id> --subscription <client-subscription-id> \
    --sql-admin-group-name "Datia SQL Admins" --sql-admin-group-id <group-object-id> --dry-run   # then without --dry-run
```

The script creates or reuses everything it can and ends with the steps that need a person: GDAP approval,
the SQL grant script, the Claude API key and spend limit, Power BI tenant settings, publishing the model,
assigning portal users and the Reviewer role, `init-db`, and the isolation test.

Portal sign-in (once for the company): a multi-tenant Entra app registration with `accessTokenAcceptedVersion`
2, "assignment required" on, app roles `Reviewer` and `Viewer`, an exposed scope `access_as_user`, and these
settings: `DATIA_PORTAL_CLIENT_ID`, `DATIA_PORTAL_API_SCOPE`, `DATIA_API_AUDIENCE` (App ID URI and client id).

## Before production (open items)

- [ ] Run `check-ecf` on real e-CF files (sale, purchase, credit note, dollar invoice) and fix any field differences.
- [ ] First real `refresh-fx` and `refresh-rnc` from Azure (the sources were confirmed but not downloadable from the
      build environment); check the Banco Central notice text matches the parser.
- [ ] First real `onboard.py` run against a test tenant and subscription; then a private endpoint for SQL.
- [ ] Portal: sign-in with a real Entra app registration; Power BI report embed with `report_id`.
- [ ] Azure Blob storage for documents (today: local folder path); email and WhatsApp intake (sending by email works).
- [ ] Confirm Claude model names in `backbone/settings.py` and Anthropic data-retention terms.
