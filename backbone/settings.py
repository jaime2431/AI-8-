"""Company-wide settings (not client data)."""
import os
from zoneinfo import ZoneInfo

# Business clock: the Dominican Republic. Azure containers run on UTC, so every "today" comes from here.
TZ = ZoneInfo(os.environ.get("DATIA_TZ", "America/Santo_Domingo"))

# Claude models. Agents that read documents use the stronger model; checks and summaries can use the fast one.
MODEL_AGENT = os.environ.get("DATIA_MODEL_AGENT", "claude-sonnet-5-5")
MODEL_FAST = os.environ.get("DATIA_MODEL_FAST", "claude-haiku-4-5-20251001")

# "dev" allows the X-Dev-Client header in the portal. Anything else requires real Microsoft Entra logins.
ENV = os.environ.get("DATIA_ENV", "prod")

# Portal API audience (the app registration's Application ID URI) for token validation.
API_AUDIENCE = os.environ.get("DATIA_API_AUDIENCE", "")

CLIENTS_FILE = os.environ.get("DATIA_CLIENTS_FILE", "config/clients.json")

# Portal sign-in (Microsoft Entra multi-tenant app registration)
PORTAL_CLIENT_ID = os.environ.get("DATIA_PORTAL_CLIENT_ID", "")
PORTAL_API_SCOPE = os.environ.get("DATIA_PORTAL_API_SCOPE", "")      # e.g. api://datia-portal/access_as_user

# Shared database for PUBLIC reference data only (DGII RNC registry). Never holds client data.
REFERENCE_DB_URL = os.environ.get("DATIA_REFERENCE_DB_URL", "")
