"""Source connectors. Each yields staging rows (dicts with the invoice fields) for ONE client.

Spec in the client config, e.g.:
  {"type": "ecf_folder", "name": "ecf", "path": "/data/ferreteria-norte/ecf"}
  {"type": "csv_folder", "name": "pos", "path": "...", "direction": "sale", "mapping": {"Fecha": "issue_date", ...}}
  {"type": "sql", "name": "erp", "url_secret": "erp-database-url", "query": "SELECT ... WHERE updated_at > :since",
   "mapping": {...}, "direction": "sale"}
"""
from __future__ import annotations

from .base import Connector, build_connector

__all__ = ["Connector", "build_connector"]
