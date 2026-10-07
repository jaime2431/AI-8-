"""Aggregates the pilot data into the figures the BI dashboard shows (pilot/out/dashboard.json).

The measures follow docs/powerbi/medidas.dax in spirit, computed here in Python so the dashboard
can be reviewed before the Power BI template exists.   python pilot/dashboard_data.py
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA, OUT = ROOT / "pilot" / "data", ROOT / "pilot" / "out"
TODAY = date(2026, 10, 7)
ECF_DEADLINE = date(2026, 11, 15)
GOAL_USD = 5000


def load(name):
    with open(DATA / name, encoding="utf-8") as f:
        return list(csv.DictReader(f))


d = lambda s: date.fromisoformat(s) if s else None
num = lambda s: float(s or 0)


def main():
    summary = json.loads((DATA / "resumen.json").read_text(encoding="utf-8"))
    ventas, compras, cobros = load("ventas.csv"), load("compras.csv"), load("cobros.csv")
    eventos, clientes, items = load("eventos.csv"), load("clientes.csv"), load("inventario_items.csv")
    movs, tasas, sups = load("inventario_movimientos.csv"), load("tasas.csv"), load("suplidores.csv")
    rate = {d(t["Fecha"]): num(t["Venta"]) for t in tasas}
    sell = lambda day: rate.get(day) or rate[max(k for k in rate if k <= day)]
    ev = {e["Evento_ID"]: e for e in eventos}
    fixed_month = sum(num(e["Salario_Mensual"]) for e in load("empleados.csv") if e["Tipo"] == "Fijo") * 1.2462
    overhead_cats = {"Alquiler local", "Electricidad", "Telecomunicaciones", "Mantenimiento"}

    # ---- quincena profit (operating, before income tax; propina is passed to staff) ----
    q = defaultdict(lambda: defaultdict(float))
    key = lambda day: f"{day.year}-{day.month:02d}-{'Q1' if day.day <= 15 else 'Q2'}"
    for e in eventos:
        if e["Estado"] != "Realizado" or d(e["Fecha_Evento"]) > date(2026, 9, 30):
            continue
        k = key(d(e["Fecha_Evento"]))
        q[k]["rev"] += int(e["Invitados"]) * num(e["Precio_Por_Persona"])
        q[k]["cost"] += num(e["Costo_Alimentos"]) + num(e["Costo_Personal"]) + num(e["Otros_Costos"])
    for c in compras:                                   # overhead from purchases, fixed payroll split per quincena
        if c["Categoria"] in overhead_cats and d(c["Fecha"]) <= date(2026, 9, 30):
            q[key(d(c["Fecha"]))]["cost"] += num(c["Subtotal"])
    waste = defaultdict(float)
    for m in movs:
        if m["Tipo_Mov"] == "Merma":
            waste[m["Fecha"][:7]] += num(m["Cantidad"]) * num(m["Costo_Unitario"])
    for k in list(q) + [f"{y}-{mm:02d}-{h}" for y, mm in [(2025, 10), (2025, 11), (2025, 12)] + [(2026, i) for i in range(1, 10)] for h in ("Q1", "Q2")]:
        q[k]["cost"] += fixed_month / 2 + waste[k[:7]] / 2
    quincenas = []
    for k in sorted(q):
        y, m, h = int(k[:4]), int(k[5:7]), k[-2:]
        mid = date(y, m, 8 if h == "Q1" else 22)
        p = q[k]["rev"] - q[k]["cost"]
        quincenas.append({"k": k, "label": f"{['ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic'][m-1]} {h[-1]}",
                          "year": y, "rev": round(q[k]["rev"]), "profit_usd": round(p / sell(mid))})

    # ---- monthly sales by event type ----
    months = [f"{y}-{m:02d}" for y, m in [(2025, 10), (2025, 11), (2025, 12)] + [(2026, i) for i in range(1, 10)]]
    by_type = {t: [0.0] * 12 for t in ("Corporativo", "Concierto", "Boda", "Social")}
    for v in ventas:
        if v["Fecha"][:7] in months and v["Evento_ID"] in ev:
            by_type[ev[v["Evento_ID"]]["Tipo_Evento"]][months.index(v["Fecha"][:7])] += num(v["Subtotal"]) * num(v["Tasa"])

    # ---- events: oversight ----
    done = [e for e in eventos if e["Estado"] == "Realizado"]
    ev_rows = []
    for e in done:
        rev = int(e["Invitados"]) * num(e["Precio_Por_Persona"])
        food, staff, other = num(e["Costo_Alimentos"]), num(e["Costo_Personal"]), num(e["Otros_Costos"])
        ev_rows.append({"id": e["Evento_ID"], "date": e["Fecha_Evento"], "type": e["Tipo_Evento"], "client": e["Cliente"],
                        "guests": int(e["Invitados"]), "rev": round(rev), "food_pct": round(food / rev * 100, 1),
                        "staff_pct": round(staff / rev * 100, 1), "margin_pct": round((rev - food - staff - other) / rev * 100, 1)})
    type_margin = {}
    for t in by_type:
        rows = [r for r in ev_rows if r["type"] == t]
        rev = sum(r["rev"] for r in rows)
        type_margin[t] = {"events": len(rows), "rev": rev,
                          "margin_pct": round(sum(r["rev"] * r["margin_pct"] for r in rows) / rev, 1) if rev else 0,
                          "ticket": round(rev / sum(r["guests"] for r in rows)) if rows else 0}

    # ---- receivables and credit ----
    credit_notes = defaultdict(float)
    for v in ventas:
        if v["Tipo_NCF"] == "B04":
            credit_notes[v["NCF_Modificado"]] += -num(v["Total_DOP"])
    paid = defaultdict(float)
    for c in cobros:
        paid[c["NCF_Factura"]] += num(c["Monto_DOP"])
    open_inv, aging = [], {"0-30": 0.0, "31-60": 0.0, "61-90": 0.0, ">90": 0.0, "Por vencer": 0.0}
    for v in ventas:
        if v["Tipo_NCF"] == "B04":
            continue
        bal = num(v["Total_DOP"]) - credit_notes[v["NCF"]] - paid[v["NCF"]]
        if bal > 1:
            late = (TODAY - d(v["Vence"])).days
            bucket = "Por vencer" if late <= 0 else "0-30" if late <= 30 else "31-60" if late <= 60 else "61-90" if late <= 90 else ">90"
            aging[bucket] += bal
            open_inv.append({"ncf": v["NCF"], "rnc": v["RNC_Cliente"], "bal": bal, "late": late})
    pay_late, pay_days = defaultdict(list), []
    inv_by_ncf = {v["NCF"]: v for v in ventas}
    for c in cobros:
        v = inv_by_ncf.get(c["NCF_Factura"])
        if v and v["Condicion"] == "Credito" and c["Tipo_Cobro"] == "Saldo":
            pay_late[v["RNC_Cliente"]].append((d(c["Fecha"]) - d(v["Vence"])).days)
            pay_days.append((d(c["Fecha"]) - d(v["Fecha"])).days)
    credit = []
    for cl in clientes:
        rnc = cl["RNC_Cliente"]
        billed6 = sum(num(v["Total_DOP"]) for v in ventas if v["RNC_Cliente"] == rnc and d(v["Fecha"]) >= date(2026, 4, 1))
        mine = [o for o in open_inv if o["rnc"] == rnc]
        debt, overdue = sum(o["bal"] for o in mine), sum(o["bal"] for o in mine if o["late"] > 0)
        worst = max([o["late"] for o in mine], default=0)
        avg_late = round(sum(pay_late[rnc]) / len(pay_late[rnc])) if pay_late[rnc] else None
        limit = num(cl["Limite_Credito"])
        # Suggested limit: six months of billing turned into a monthly figure, times the terms, times a
        # payment-behaviour factor; never more than 1.5x the current limit; zero with anything over 90 days.
        factor = 1.2 if (avg_late or 0) <= 5 else 1.0 if avg_late <= 15 else 0.7 if avg_late <= 30 else 0.4
        suggested = 0 if worst > 90 else min(limit * 1.5, billed6 / 6 * int(cl["Dias_Credito"]) / 30 * factor * 2)
        suggested = round(suggested / 10000) * 10000
        status = ("Suspender crédito" if worst > 90 or (avg_late or 0) > 60 else "Excede límite" if debt > limit
                  else "Revisar" if worst > 30 or (avg_late or 0) > 20 else "Al día")
        credit.append({"client": cl["Cliente"], "type": cl["Tipo_Cliente"], "debt": round(debt), "overdue": round(overdue),
                       "worst_late": worst, "avg_late": avg_late, "limit": round(limit), "suggested": suggested, "status": status})
    credit.sort(key=lambda r: (-r["overdue"], -r["debt"]))
    paid_days = [x for v in pay_late.values() for x in v]

    # ---- inventory ----
    stock_value, below, waste_by_cat, out_by_cat = defaultdict(float), [], defaultdict(float), defaultdict(float)
    item_cat = {i["Codigo"]: i["Categoria"] for i in items}
    use_90 = defaultdict(float)
    for m in movs:
        cat = item_cat[m["Codigo"]]
        val = num(m["Cantidad"]) * num(m["Costo_Unitario"])
        if m["Tipo_Mov"] == "Merma":
            waste_by_cat[cat] += val
        if m["Tipo_Mov"] == "Salida":
            out_by_cat[cat] += val
            if d(m["Fecha"]) > date(2026, 7, 8):
                use_90[m["Codigo"]] += num(m["Cantidad"])
    for i in items:
        stock_value[i["Categoria"]] += num(i["Stock_Actual"]) * num(i["Costo_Unitario"])
        daily = use_90[i["Codigo"]] / 91
        days_left = round(num(i["Stock_Actual"]) / daily) if daily else None
        if num(i["Stock_Actual"]) < num(i["Stock_Minimo"]):
            sup = next(s["Suplidor"] for s in sups if s["RNC_Suplidor"] == i["Suplidor_Principal"])
            below.append({"code": i["Codigo"], "item": i["Producto"], "unit": i["Unidad"], "stock": num(i["Stock_Actual"]),
                          "min": num(i["Stock_Minimo"]), "days_left": days_left, "supplier": sup,
                          "reorder": round(num(i["Stock_Minimo"]) * 2.5 - num(i["Stock_Actual"]), 1)})
    waste = [{"cat": c, "pct": round(waste_by_cat[c] / (waste_by_cat[c] + out_by_cat[c]) * 100, 1), "value": round(waste_by_cat[c])}
             for c in waste_by_cat]
    waste.sort(key=lambda r: -r["pct"])

    # ---- purchases ----
    top = defaultdict(float)
    for c in compras:
        if d(c["Fecha"]) <= date(2026, 9, 30):
            top[c["Suplidor"]] += num(c["Total_DOP"])
    top_sup = sorted(({"supplier": k, "total": round(v)} for k, v in top.items()), key=lambda r: -r["total"])[:8]
    price = defaultdict(dict)
    for m in movs:
        if m["Codigo"] in ("PRO-002", "PRO-003", "MAR-001") and m["Tipo_Mov"] == "Entrada":
            price[m["Codigo"]][m["Fecha"][:7]] = num(m["Costo_Unitario"])
    price_trend = {code: [price[code].get(mm) for mm in months] for code in price}

    # ---- e-CF readiness ----
    comp12 = [c for c in compras if d(c["Fecha"]) <= date(2026, 9, 30)]
    ecf_buy_amt = sum(num(c["Total_DOP"]) for c in comp12 if c["Tipo_NCF"].startswith("E"))
    ecf = {"days_left": (ECF_DEADLINE - TODAY).days,
           "purchases_ecf_pct": round(ecf_buy_amt / sum(num(c["Total_DOP"]) for c in comp12) * 100, 1),
           "purchases_ecf_docs": sum(1 for c in comp12 if c["Tipo_NCF"].startswith("E")), "purchases_docs": len(comp12),
           "sales_ecf_pct": 0.0,
           "suppliers_ecf": sum(1 for s in sups if s["Emite_eCF"] == "Si"), "suppliers": len(sups),
           "b01_used": sum(1 for v in ventas if v["Tipo_NCF"] == "B01"), "b02_used": sum(1 for v in ventas if v["Tipo_NCF"] == "B02"),
           "b11_used": sum(1 for c in compras if c["Tipo_NCF"] == "B11")}
    pipe = json.loads((OUT / "pipeline_septiembre_2026.json").read_text(encoding="utf-8"))

    # ---- procedures ----
    def avg(xs):
        xs = [x for x in xs if x is not None]
        return round(sum(xs) / len(xs), 1) if xs else None
    proc = {"quote_to_contract": avg([(d(e["Fecha_Contrato"]) - d(e["Fecha_Cotizacion"])).days for e in done if e["Fecha_Contrato"]]),
            "event_to_invoice": avg([(d(e["Fecha_Factura"]) - d(e["Fecha_Evento"])).days for e in done if e["Fecha_Factura"]]),
            "late_invoices": sum(1 for e in done if e["Fecha_Factura"] and (d(e["Fecha_Factura"]) - d(e["Fecha_Evento"])).days > 3),
            "invoice_to_cash": avg(pay_days), "avg_days_late": avg(paid_days),
            "deposit_by_type": {t: round(sum(1 for e in done if e["Tipo_Evento"] == t and e["Fecha_Anticipo"]) /
                                         max(1, sum(1 for e in done if e["Tipo_Evento"] == t)) * 100) for t in by_type},
            "events_done": len(done)}
    booked = [e for e in eventos if e["Estado"] in ("Contratado", "Cotizado")]
    agenda = [{"month": mm, "contratado": round(sum(int(e["Invitados"]) * num(e["Precio_Por_Persona"]) for e in booked if e["Fecha_Evento"][:7] == mm and e["Estado"] == "Contratado")),
               "cotizado": round(sum(int(e["Invitados"]) * num(e["Precio_Por_Persona"]) for e in booked if e["Fecha_Evento"][:7] == mm and e["Estado"] == "Cotizado"))}
              for mm in ("2026-10", "2026-11", "2026-12")]

    out = {"company": summary["company"], "today": TODAY.isoformat(), "rate": summary["rate_today"],
           "kpi": {"revenue_12m": summary["revenue_dop_12m"], "profit_12m": summary["profit_dop_12m"], "margin": summary["margin"],
                   "avg_profit_usd": summary["avg_profit_usd_quincena"], "below_goal": summary["quincenas_below_goal"],
                   "events": len(done), "ar_total": round(sum(aging.values())), "ar_overdue": round(sum(v for k, v in aging.items() if k != "Por vencer")),
                   "inventory_value": round(sum(stock_value.values())), "below_min": len(below)},
           "months": months, "quincenas": quincenas, "sales_by_type": {k: [round(x) for x in v] for k, v in by_type.items()},
           "type_margin": type_margin, "events": sorted(ev_rows, key=lambda r: r["date"]),
           "aging": {k: round(v) for k, v in aging.items()}, "credit": credit,
           "inventory": {"by_cat": {k: round(v) for k, v in stock_value.items()}, "below": below, "waste": waste},
           "top_suppliers": top_sup, "price_trend": price_trend, "ecf": ecf,
           "pipeline": {"promote": pipe["promote"], "review": pipe["review_queue"], "rejected": pipe["rejected"],
                        "dgii": {k: {"file": v["file"], "lines": v["lines"], "warnings": v["warnings"]} for k, v in pipe["dgii"].items()}},
           "process": proc, "agenda": agenda}
    (OUT / "dashboard.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    k = out["kpi"]
    print(json.dumps(k, indent=1), "\naging", out["aging"], "\nproc", proc, "\nbelow", [b["item"] for b in below],
          "\nwaste", waste[:3], "\ncredit", [(c["client"][:22], c["debt"], c["overdue"], c["avg_late"], c["status"], c["suggested"]) for c in credit[:6]],
          "\ntype", type_margin, "\necf", ecf, "\nagenda", agenda, "\nq-min/max", min(x["profit_usd"] for x in quincenas), max(x["profit_usd"] for x in quincenas), len(quincenas))


def build_page():
    """Bakes dashboard.json into the page template -> docs/pilot-dashboard.html."""
    data = (OUT / "dashboard.json").read_text(encoding="utf-8").replace("</", "<\\/")
    page = (ROOT / "pilot" / "dashboard_template.html").read_text(encoding="utf-8").replace("__DATA__", data)
    (ROOT / "docs" / "pilot-dashboard.html").write_text(page, encoding="utf-8")


if __name__ == "__main__":
    main()
    build_page()
