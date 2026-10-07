"""Runs the pilot's September 2026 export through the real backbone, as a client's night would:
CSV export + e-CF folder -> staging -> validation rules -> clean invoices / review queue -> DGII 606/607.

    python pilot/run_pipeline.py          (after python pilot/generate.py)

Uses a throwaway SQLite database; writes the results to pilot/out/.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import sys
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import func, insert, select  # noqa: E402

from backbone.context import ClientContext  # noqa: E402
from backbone.db import clean_invoices, create_tables, fx_rates, review_queue, staging_invoices  # noqa: E402
from backbone.dgii_formats import build_606, build_607, file_name  # noqa: E402
from backbone.pipeline import extract_sources, promote  # noqa: E402
from backbone.tenancy import ClientRegistry  # noqa: E402

DATA, OUT = ROOT / "pilot" / "data", ROOT / "pilot" / "out"
RUN_DATE = date(2026, 10, 7)


def main():
    summary = json.loads((DATA / "resumen.json").read_text(encoding="utf-8"))
    own = summary["company"]["rnc"]
    work = Path(tempfile.mkdtemp(prefix="datia-pilot-"))
    for sub in ("erp_ventas", "erp_compras", "ecf", "inbox"):
        (work / sub).mkdir()
    shutil.copy(DATA / "export_erp" / "ventas_2026-09.csv", work / "erp_ventas")
    shutil.copy(DATA / "export_erp" / "compras_2026-09.csv", work / "erp_compras")
    for f in (DATA / "ecf").glob("*.xml"):
        shutil.copy(f, work / "ecf")
    config = {"clients": [{
        "client_id": "pilot", "name": summary["company"]["name"], "own_rnc": own, "entra_tenant_id": "tid-pilot",
        "inbox_path": str(work / "inbox"), "open_period_start": "2026-01-01",
        "connectors": [
            {"type": "ecf_folder", "name": "ecf", "path": str(work / "ecf")},
            {"type": "csv_folder", "name": "erp_ventas", "path": str(work / "erp_ventas"), "direction": "sale",
             "mapping": {"Fecha": "issue_date", "NCF": "ncf", "NCF Modificado": "ncf_modified", "RNC Emisor": "issuer_rnc",
                         "RNC Cliente": "buyer_rnc", "Subtotal": "subtotal", "ITBIS": "itbis", "Propina": "tip",
                         "Total": "total", "Moneda": "currency"}},
            {"type": "csv_folder", "name": "erp_compras", "path": str(work / "erp_compras"), "direction": "purchase",
             "mapping": {"Fecha": "issue_date", "NCF": "ncf", "RNC Suplidor": "issuer_rnc", "Suplidor": "issuer_name",
                         "RNC Comprador": "buyer_rnc", "Subtotal": "subtotal", "ITBIS": "itbis",
                         "Otros Impuestos": "other_taxes", "Total": "total"}},
        ]}]}
    (work / "clients.json").write_text(json.dumps(config))
    os.environ["DATIA__PILOT__DATABASE_URL"] = f"sqlite:///{work / 'pilot.db'}"
    os.environ["DATIA__PILOT__ANTHROPIC_API_KEY"] = "not-used"
    ctx = ClientContext(ClientRegistry.from_file(work / "clients.json").get("pilot"))
    create_tables(ctx.engine)

    with ctx.engine.begin() as conn:            # the Banco Central sell rate, as refresh-fx would load it
        for row in csv.DictReader(open(DATA / "tasas.csv", encoding="utf-8")):
            conn.execute(insert(fx_rates).values(rate_date=date.fromisoformat(row["Fecha"]), usd_dop=Decimal(row["Venta"])))

    extracted = extract_sources(ctx)
    counts = promote(ctx, RUN_DATE)

    with ctx.engine.connect() as conn:
        review = [dict(r) for r in conn.execute(
            select(staging_invoices.c.ncf, staging_invoices.c.issuer_name, staging_invoices.c.source, review_queue.c.reason)
            .join(review_queue, review_queue.c.staging_id == staging_invoices.c.id)).mappings()]
        rejected = [dict(r) for r in conn.execute(
            select(staging_invoices.c.ncf, staging_invoices.c.issuer_name, staging_invoices.c.rules_json)
            .where(staging_invoices.c.status == "rejected")).mappings()]
        totals = {d: float(conn.execute(select(func.sum(clean_invoices.c.total_dop)).where(clean_invoices.c.direction == d)).scalar() or 0)
                  for d in ("sale", "purchase")}
        rows = [dict(r) for r in conn.execute(select(clean_invoices)).mappings()]

    OUT.mkdir(exist_ok=True)
    dgii = {}
    for fmt, builder in (("606", build_606), ("607", build_607)):
        text, warnings = builder(rows, own, "2026-09")
        name = file_name(fmt, own, "2026-09")
        (OUT / name).write_bytes(text.encode("utf-8"))
        dgii[fmt] = {"file": name, "lines": text.count("\n"), "warnings": len(warnings), "sample_warnings": warnings[:3]}

    result = {"run_date": RUN_DATE.isoformat(), "extracted": extracted, "promote": counts,
              "clean_totals_dop": totals, "review_queue": review,
              "rejected": [{"ncf": r["ncf"], "issuer": r["issuer_name"],
                            "why": [x["message"] for x in json.loads(r["rules_json"]) if x["outcome"] == "reject"]} for r in rejected],
              "dgii": dgii}
    (OUT / "pipeline_septiembre_2026.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
