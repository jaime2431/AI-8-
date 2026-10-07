"""Builds the pilot company's data: a fictitious Dominican event caterer with realistic numbers.

Sabor & Escena Catering SRL: central kitchen in Santo Domingo Este, 12 fixed staff and 88 people on
the per-event roster, catering for corporate events, concerts, weddings and social events.
Target set by the owner: US$5,000 operating profit every quincena at a 30% margin.

Everything is invented (companies, people, RNCs) except the Banco Central rate of Oct 7, 2026, which
the client gave us (buy 60.8522, sell 61.4111). Earlier rates are a synthetic walk ending on it.
The same seed always produces the same files:  python pilot/generate.py
"""
from __future__ import annotations

import csv
from xml.sax.saxutils import escape
import json
import math
import random
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backbone.validation import rnc_checksum_ok  # noqa: E402

rng = random.Random(20261007)
OUT = Path(__file__).parent / "data"
START, END, TODAY = date(2025, 10, 1), date(2026, 9, 30), date(2026, 10, 7)
HORIZON = date(2026, 12, 31)                 # booked and quoted events after today
RATE_TODAY = (60.8522, 61.4111)              # Banco Central, Oct 7 2026 (from the client)
GOAL_USD_QUINCENA = 5000
TARGET_MARGIN = 0.30
ITBIS, PROPINA = 0.18, 0.10


def days(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def r2(x: float) -> float:
    return round(x + 1e-9, 2)


# ---------- identifiers that pass the DGII checks ----------

def new_rnc(prefix: str = "1", _used=set()) -> str:
    while True:
        base = prefix + "".join(rng.choice("0123456789") for _ in range(7))
        for c in "0123456789":
            if rnc_checksum_ok(base + c) and base + c not in _used:
                _used.add(base + c)
                return base + c


def new_cedula(_used=set()) -> str:
    while True:
        base = rng.choice(["001", "402", "223", "031"]) + "".join(rng.choice("0123456789") for _ in range(7))
        for c in "0123456789":
            if rnc_checksum_ok(base + c) and base + c not in _used:
                _used.add(base + c)
                return base + c


OWN_RNC = new_rnc("1")
COMPANY = {"name": "Sabor & Escena Catering SRL (ficticia)", "rnc": OWN_RNC, "city": "Santo Domingo Este",
           "erp": "Monica 10 (facturación e inventario) + Excel; e-CF aún no emitido",
           "employees_fixed": 12, "employees_per_event": 88}


# ---------- exchange rates: synthetic walk that ends on the real Oct 7 rate ----------

def build_rates() -> dict[date, tuple[float, float]]:
    out, n = {}, (TODAY - START).days
    start_sell, noise = 59.35, 0.0
    for i, d in enumerate(days(START, TODAY)):
        if d.weekday() >= 5 and out:                       # weekends keep Friday's rate
            out[d] = out[d - timedelta(days=1)]
            continue
        noise = noise * 0.85 + rng.uniform(-0.06, 0.06)
        sell = start_sell + (RATE_TODAY[1] - start_sell) * i / n + noise * (1 - i / n)
        out[d] = (round(sell - 0.5589, 4), round(sell, 4))
    out[TODAY] = RATE_TODAY
    return out


RATES = build_rates()
rate_sell = lambda d: RATES[min(d, TODAY)][1]


# ---------- master data ----------

FIRST = "María José Ana Luis Carmen Juan Rosa Pedro Yolanda Ramón Altagracia Miguel Juana Rafael Esther Francisco Mercedes Carlos Ángela Manuel Yokasta Wilson Dahiana Félix".split()
LAST = "Rodríguez Pérez Martínez García Fernández Reyes Santos Jiménez Díaz Peña Batista Castillo Rosario Ramírez Vásquez Guzmán Núñez Polanco Mejía Tavárez Almonte Germán".split()
person = lambda: f"{rng.choice(FIRST)} {rng.choice(LAST)} {rng.choice(LAST)}"

# (name, type, sector, credit limit RD$, credit days, payer profile: days late (min, max))
CLIENT_SPECS = [
    ("Banco Comercial del Ozama SA", "Empresa", "Banca", 900_000, 30, (-3, 6)),
    ("Constructora Bávaro Hills SRL", "Empresa", "Construcción", 500_000, 45, (5, 25)),
    ("Telecomunicaciones del Caribe SA", "Empresa", "Telecom", 800_000, 30, (0, 10)),
    ("Seguros Bahía Azul SA", "Empresa", "Seguros", 600_000, 30, (-2, 5)),
    ("Farmacéutica Quisqueya SRL", "Empresa", "Salud", 450_000, 45, (2, 14)),
    ("Industrias Plásticas del Cibao SRL", "Empresa", "Manufactura", 350_000, 30, (10, 35)),
    ("Grupo Hotelero Costa Ámbar SA", "Empresa", "Turismo", 700_000, 60, (0, 12)),
    ("Universidad Tecnológica del Este", "Empresa", "Educación", 400_000, 45, (15, 40)),
    ("Textiles Zona Este SRL", "Empresa", "Zona franca", 300_000, 30, (0, 8)),
    ("Distribuidora Automotriz Duarte SRL", "Empresa", "Comercio", 450_000, 30, (3, 15)),
    ("Agroexportadora Valle de Constanza SRL", "Empresa", "Agroexportación", 250_000, 30, (20, 50)),
    ("Consultores Jurídicos Gazcue SRL", "Empresa", "Servicios", 200_000, 30, (-5, 3)),
    ("Producciones Merengue Live SRL", "Promotora", "Entretenimiento", 1_200_000, 30, (5, 20)),
    ("Caribe Stage Events SRL", "Promotora", "Entretenimiento", 1_000_000, 30, (0, 12)),
    ("Bachata Fest Producciones SRL", "Promotora", "Entretenimiento", 800_000, 30, (70, 150)),
    ("Global Tours Dominicana SRL", "Promotora", "Entretenimiento", 1_500_000, 45, (0, 10)),   # bills in USD
]
CLIENTS = [{"rnc": new_rnc(rng.choice("14")), "name": n, "type": t, "sector": s, "limit": lim, "days": dd, "late": late}
           for n, t, s, lim, dd, late in CLIENT_SPECS]
USD_CLIENTS = {"Global Tours Dominicana SRL"}

# (name, category, issues e-CF, credit days, informal seller -> we issue B11)
SUPPLIER_SPECS = [
    ("Avícola del Cibao SRL", "Proteínas", True, 15, False),
    ("Carnes Selectas Quisqueya SRL", "Proteínas", False, 15, False),
    ("Mariscos Samaná SRL", "Mariscos", False, 0, False),
    ("Vegetales Frescos Constanza (productor)", "Vegetales y frutas", False, 0, True),
    ("Distribuidora de Víveres Ozama SRL", "Granos y víveres", False, 15, False),
    ("Lácteos La Vega SRL", "Lácteos", False, 15, False),
    ("Bebidas del Caribe SA", "Bebidas no alcohólicas", True, 30, False),
    ("Licores y Vinos Colonial SRL", "Bebidas alcohólicas", True, 30, False),
    ("Desechables Santo Domingo SRL", "Desechables", False, 30, False),
    ("Químicos y Limpieza Duarte SRL", "Limpieza", False, 30, False),
    ("Gases del Este SRL", "Gas", True, 0, False),
    ("Alquileres Eventos Premium SRL", "Alquiler mobiliario", False, 30, False),
    ("Transporte Express Ozama SRL", "Transporte", False, 15, False),
    ("Inmobiliaria Los Mina SRL", "Alquiler local", False, 0, False),
    ("Empresa Eléctrica Metropolitana (ficticia)", "Electricidad", True, 0, False),
    ("Telecable Antillas SA", "Telecomunicaciones", True, 0, False),
    ("Servicios Técnicos de Cocina SRL", "Mantenimiento", False, 30, False),
]
SUPPLIERS = {n: {"rnc": new_cedula() if informal else new_rnc(rng.choice("14")), "name": n, "cat": c, "ecf": e,
                 "days": dd, "informal": informal} for n, c, e, dd, informal in SUPPLIER_SPECS}

# code, product, category, unit, unit cost RD$ (Oct 2025), ITBIS rate, weight inside its category, supplier
ITEMS = [
    ("PRO-001", "Pollo entero", "Proteínas", "lb", 92, 0, 3, "Avícola del Cibao SRL"),
    ("PRO-002", "Pechuga de pollo", "Proteínas", "lb", 162, 0, 3, "Avícola del Cibao SRL"),
    ("PRO-003", "Filete de res", "Proteínas", "lb", 410, 0, 2, "Carnes Selectas Quisqueya SRL"),
    ("PRO-004", "Pernil de cerdo", "Proteínas", "lb", 142, 0, 2, "Carnes Selectas Quisqueya SRL"),
    ("PRO-005", "Huevos (cartón 30)", "Proteínas", "cartón", 255, 0, 1, "Avícola del Cibao SRL"),
    ("MAR-001", "Camarones", "Mariscos", "lb", 470, 0, 2, "Mariscos Samaná SRL"),
    ("MAR-002", "Filete de dorado", "Mariscos", "lb", 255, 0, 2, "Mariscos Samaná SRL"),
    ("VEG-001", "Plátanos", "Vegetales y frutas", "unidad", 18, 0, 2, "Vegetales Frescos Constanza (productor)"),
    ("VEG-002", "Yuca", "Vegetales y frutas", "lb", 30, 0, 1, "Vegetales Frescos Constanza (productor)"),
    ("VEG-003", "Papas", "Vegetales y frutas", "lb", 40, 0, 1, "Vegetales Frescos Constanza (productor)"),
    ("VEG-004", "Cebolla", "Vegetales y frutas", "lb", 58, 0, 1, "Vegetales Frescos Constanza (productor)"),
    ("VEG-005", "Tomate", "Vegetales y frutas", "lb", 52, 0, 1, "Vegetales Frescos Constanza (productor)"),
    ("VEG-006", "Lechuga", "Vegetales y frutas", "unidad", 55, 0, 1, "Vegetales Frescos Constanza (productor)"),
    ("VEG-007", "Aguacate", "Vegetales y frutas", "unidad", 45, 0, 1, "Vegetales Frescos Constanza (productor)"),
    ("VEG-008", "Frutas variadas", "Vegetales y frutas", "lb", 65, 0, 2, "Vegetales Frescos Constanza (productor)"),
    ("GRA-001", "Arroz selecto", "Granos y víveres", "lb", 38, 0, 3, "Distribuidora de Víveres Ozama SRL"),
    ("GRA-002", "Habichuelas rojas", "Granos y víveres", "lb", 84, 0, 2, "Distribuidora de Víveres Ozama SRL"),
    ("GRA-003", "Aceite vegetal (galón)", "Granos y víveres", "galón", 690, 0.16, 2, "Distribuidora de Víveres Ozama SRL"),
    ("GRA-004", "Azúcar crema", "Granos y víveres", "lb", 42, 0.16, 1, "Distribuidora de Víveres Ozama SRL"),
    ("GRA-005", "Café molido", "Granos y víveres", "lb", 380, 0.16, 1, "Distribuidora de Víveres Ozama SRL"),
    ("LAC-001", "Queso de freír", "Lácteos", "lb", 275, 0, 2, "Lácteos La Vega SRL"),
    ("LAC-002", "Leche (litro)", "Lácteos", "litro", 72, 0, 1, "Lácteos La Vega SRL"),
    ("LAC-003", "Mantequilla", "Lácteos", "lb", 255, 0.16, 1, "Lácteos La Vega SRL"),
    ("BEB-001", "Refrescos (caja 24)", "Bebidas no alcohólicas", "caja", 715, 0.18, 3, "Bebidas del Caribe SA"),
    ("BEB-002", "Agua 16 oz (caja 24)", "Bebidas no alcohólicas", "caja", 255, 0.18, 3, "Bebidas del Caribe SA"),
    ("BEB-003", "Hielo (funda 10 lb)", "Bebidas no alcohólicas", "funda", 70, 0.18, 2, "Bebidas del Caribe SA"),
    ("BEB-004", "Jugos naturales (galón)", "Bebidas no alcohólicas", "galón", 340, 0.18, 1, "Bebidas del Caribe SA"),
    ("ALC-001", "Cerveza nacional (caja 24)", "Bebidas alcohólicas", "caja", 2350, 0.18, 4, "Licores y Vinos Colonial SRL"),
    ("ALC-002", "Ron añejo 700 ml", "Bebidas alcohólicas", "botella", 640, 0.18, 3, "Licores y Vinos Colonial SRL"),
    ("ALC-003", "Whisky 750 ml", "Bebidas alcohólicas", "botella", 1850, 0.18, 1, "Licores y Vinos Colonial SRL"),
    ("ALC-004", "Vino tinto 750 ml", "Bebidas alcohólicas", "botella", 740, 0.18, 2, "Licores y Vinos Colonial SRL"),
    ("DES-001", "Vasos desechables (paq. 50)", "Desechables", "paquete", 118, 0.18, 2, "Desechables Santo Domingo SRL"),
    ("DES-002", "Platos desechables (paq. 25)", "Desechables", "paquete", 158, 0.18, 2, "Desechables Santo Domingo SRL"),
    ("DES-003", "Servilletas (paq. 500)", "Desechables", "paquete", 228, 0.18, 1, "Desechables Santo Domingo SRL"),
    ("DES-004", "Papel aluminio (rollo)", "Desechables", "rollo", 445, 0.18, 1, "Desechables Santo Domingo SRL"),
    ("LIM-001", "Detergente industrial (galón)", "Limpieza", "galón", 375, 0.18, 1, "Químicos y Limpieza Duarte SRL"),
    ("GAS-001", "Gas GLP (galón)", "Gas", "galón", 137, 0, 1, "Gases del Este SRL"),
]
ITEM = {i[0]: dict(zip(("code", "name", "cat", "unit", "cost", "itbis", "w", "supplier"), i)) for i in ITEMS}
PERISHABLE = {"Proteínas", "Mariscos", "Vegetales y frutas", "Lácteos"}
WASTE = {"Mariscos": (0.08, 0.13), "Vegetales y frutas": (0.05, 0.08), "Proteínas": (0.02, 0.04), "Lácteos": (0.02, 0.05)}

# share of an event's food & beverage cost by category
MIX = {
    "Corporativo": {"Proteínas": .30, "Mariscos": .05, "Vegetales y frutas": .10, "Granos y víveres": .08, "Lácteos": .05,
                    "Bebidas no alcohólicas": .12, "Bebidas alcohólicas": .15, "Desechables": .08, "Limpieza": .02, "Gas": .05},
    "Concierto": {"Proteínas": .25, "Mariscos": .02, "Vegetales y frutas": .08, "Granos y víveres": .10, "Lácteos": .03,
                  "Bebidas no alcohólicas": .18, "Bebidas alcohólicas": .20, "Desechables": .09, "Limpieza": .02, "Gas": .03},
    "Boda": {"Proteínas": .28, "Mariscos": .10, "Vegetales y frutas": .10, "Granos y víveres": .06, "Lácteos": .05,
             "Bebidas no alcohólicas": .08, "Bebidas alcohólicas": .20, "Desechables": .06, "Limpieza": .02, "Gas": .05},
    "Social": {"Proteínas": .30, "Mariscos": .04, "Vegetales y frutas": .10, "Granos y víveres": .10, "Lácteos": .05,
               "Bebidas no alcohólicas": .12, "Bebidas alcohólicas": .17, "Desechables": .07, "Limpieza": .02, "Gas": .03},
}
# guests (min, mode, max), price per guest RD$ (min, max), guests per staff member, base food cost share, base events/month
EVENT_TYPES = {
    "Corporativo": ((60, 140, 350), (1450, 2100), 14, 0.25, 4.4),
    "Concierto": ((200, 380, 900), (1100, 1600), 18, 0.23, 1.1),
    "Boda": ((100, 170, 300), (2000, 2900), 10, 0.27, 1.5),
    "Social": ((40, 80, 150), (1300, 1900), 15, 0.26, 3.2),
}
SEASON = {10: 1.0, 11: 1.15, 12: 1.55, 1: 0.6, 2: 0.85, 3: 0.9, 4: 0.85, 5: 1.1, 6: 1.05, 7: 1.15, 8: 1.05, 9: 0.8}
VENUES = {"Corporativo": ["Santo Domingo – Piantini", "Santo Domingo – Naco", "Santiago", "Juan Dolio", "Santo Domingo – Bella Vista"],
          "Concierto": ["Anfiteatro, Santo Domingo", "Estadio, Santiago", "Punta Cana", "La Romana"],
          "Boda": ["Zona Colonial", "Jarabacoa", "Punta Cana", "Juan Dolio", "Santo Domingo – Arroyo Hondo"],
          "Social": ["Santo Domingo Este", "Santo Domingo – Los Prados", "Boca Chica", "San Cristóbal"]}

FIXED_STAFF = [("Chef ejecutivo", "Cocina", 55000), ("Sous chef", "Cocina", 38000), ("Cocinero", "Cocina", 26000),
               ("Cocinero", "Cocina", 26000), ("Cocinero", "Cocina", 26000), ("Ayudante de cocina", "Cocina", 22000),
               ("Ayudante de cocina", "Cocina", 22000), ("Coordinadora de eventos", "Ventas y eventos", 32000),
               ("Ejecutivo de ventas (más comisión)", "Ventas y eventos", 28000), ("Encargada administrativa y contable", "Administración", 32000),
               ("Chofer", "Logística", 22000), ("Encargado de almacén", "Logística", 22000)]
ROSTER = [("Mesero", 40, 1800), ("Bartender", 10, 2200), ("Cocinero auxiliar", 12, 2500),
          ("Montaje y logística", 14, 1600), ("Steward", 8, 1500), ("Supervisor de evento", 4, 3200)]
EMPLOYER_CHARGES = 1.2462        # TSS employer share, INFOTEP, riesgos laborales, regalía accrual
FIXED_OVERHEAD = {               # monthly, RD$ before ITBIS (category, supplier, base, ITBIS, other taxes rate)
    "Alquiler local": ("Inmobiliaria Los Mina SRL", 85000, 0.18, 0),
    "Electricidad": ("Empresa Eléctrica Metropolitana (ficticia)", 38000, 0, 0),
    "Telecomunicaciones": ("Telecable Antillas SA", 6500, 0.18, 0.12),      # ISC 10% + CDT 2%
    "Mantenimiento": ("Servicios Técnicos de Cocina SRL", 14000, 0.18, 0),
}


# ---------- events ----------

def tri(lo, mode, hi):
    return int(rng.triangular(lo, hi, mode))


def pick_client(kind):
    if kind == "Concierto":
        return rng.choice([c for c in CLIENTS if c["type"] == "Promotora"])
    if kind == "Corporativo":
        return rng.choices([c for c in CLIENTS if c["type"] == "Empresa"],
                           weights=[5, 3, 4, 3, 2, 2, 3, 2, 2, 2, 1, 1])[0]
    return {"rnc": new_cedula(), "name": person(), "type": "Particular", "sector": "Particular", "limit": 0, "days": 0,
            "late": (0, 0)}


def build_events(price_k: float, volume: float) -> list[dict]:
    rng.seed(1)                                  # identical event calendar for every calibration pass
    events, n = [], 0
    month = date(START.year, START.month, 1)
    while month <= HORIZON:
        nxt = date(month.year + month.month // 12, month.month % 12 + 1, 1)
        for kind, (g, price, per_staff, food, base) in EVENT_TYPES.items():
            lam = base * volume * SEASON[month.month] * (2.0 if kind == "Corporativo" and month.month == 12 else 1.0)
            count = sum(1 for _ in range(40) if rng.random() < lam / 40)
            for _ in range(count):
                d = month + timedelta(days=rng.randrange((nxt - month).days))
                if kind in ("Boda", "Social", "Concierto") and d.weekday() < 4 and rng.random() < 0.7:
                    d += timedelta(days=(5 - d.weekday()))           # mostly Friday-Sunday
                if d > HORIZON:
                    continue
                n += 1
                c = pick_client(kind)
                guests = tri(*g)
                ppp = round(rng.uniform(*price) * price_k / 50) * 50
                staff = math.ceil(guests / per_staff) + (4 if kind == "Concierto" else 2)
                food_share = rng.gauss(food, 0.018)
                if rng.random() < 0.06:
                    food_share += rng.uniform(0.07, 0.12)              # overrun: last-minute menu change, waste
                quote = d - timedelta(days=rng.randint(15, 75))
                contract = quote + timedelta(days=rng.randint(2, 14))
                deposit_policy = {"Boda": 0.97, "Social": 0.9, "Concierto": 0.85, "Corporativo": 0.35}[kind]
                has_dep = rng.random() < deposit_policy
                future = d > TODAY
                status = "Realizado" if not future else ("Contratado" if contract <= TODAY else "Cotizado")
                events.append({
                    "id": f"EV-{n:04d}", "date": d, "kind": kind, "client": c, "guests": guests, "ppp": ppp,
                    "staff": staff, "food_share": food_share, "quote": quote,
                    "contract": contract if contract <= TODAY else None,
                    "deposit_date": contract + timedelta(days=rng.randint(0, 7)) if has_dep and contract <= TODAY else None,
                    "venue": rng.choice(VENUES[kind]), "status": status,
                    "usd": c["name"] in USD_CLIENTS})
        month = nxt
    return sorted(events, key=lambda e: e["date"])


def staff_cost(e) -> float:
    roles = [(r, pay) for r, cnt, pay in ROSTER for _ in range(cnt)]
    weights = [cnt for _, cnt, _ in ROSTER]
    pays = [rng.choices(ROSTER, weights=weights)[0][2] for _ in range(e["staff"])]
    return float(sum(pays)) * (1.25 if e["kind"] == "Concierto" else 1.0)     # long concert shifts


# ---------- the simulation ----------

def simulate(price_k: float, volume: float) -> dict:
    overhead_k = 1.0
    events = build_events(price_k, volume)
    rng.seed(2)
    price_drift = lambda cat, d: 1 + ((d - START).days / 365) * {"Proteínas": 0.07, "Mariscos": 0.05, "Bebidas alcohólicas": 0.04}.get(cat, 0.025)
    unit_cost = lambda code, d: r2(ITEM[code]["cost"] * price_drift(ITEM[code]["cat"], d))

    moves, usage_week = [], defaultdict(float)
    for e in events:
        if e["status"] != "Realizado":
            continue
        e["sub"] = r2(e["guests"] * e["ppp"])
        target = e["sub"] * e["food_share"]
        food = 0.0
        for cat, share in MIX[e["kind"]].items():
            items = [i for i in ITEM.values() if i["cat"] == cat]
            tw = sum(i["w"] for i in items)
            for i in items:
                c = unit_cost(i["code"], e["date"])
                qty = round(target * share * i["w"] / tw / c, 1)
                if qty <= 0:
                    continue
                moves.append({"date": e["date"], "code": i["code"], "type": "Salida", "qty": qty, "event": e["id"],
                              "ref": e["id"], "cost": c})
                food += qty * c
                usage_week[i["code"]] += qty / 52
        e["food"] = r2(food)
        e["staff_cost"] = r2(staff_cost(e))
        e["other"] = r2(e["sub"] * rng.uniform(0.035, 0.06))          # transport, furniture rental, extra gas

    # weekly replenishment and waste; stock simulated day by day
    stock = {c: round(usage_week[c] * 1.6, 1) for c in ITEM}
    minimum = {c: round(usage_week[c] * 0.6, 1) for c in ITEM}
    by_day = defaultdict(list)
    for m in moves:
        by_day[m["date"]].append(m)
    purchases, waste_cost = [], defaultdict(float)
    late_supplier = {"Mariscos Samaná SRL", "Licores y Vinos Colonial SRL"}    # last order not delivered yet
    for d in days(START, TODAY):
        if d.weekday() == 0:                                  # Monday orders
            orders = defaultdict(list)
            for code, i in ITEM.items():
                par = usage_week[code] * (1.4 if i["cat"] in PERISHABLE else 2.2)
                if stock[code] < par:
                    if d > TODAY - timedelta(days=12) and i["supplier"] in late_supplier:
                        continue
                    qty = round(par - stock[code] + usage_week[code] * 0.2, 1)
                    orders[i["supplier"]].append((code, qty, unit_cost(code, d)))
            for sup, lines in orders.items():
                for code, qty, c in lines:
                    stock[code] = round(stock[code] + qty, 1)
                    moves.append({"date": d, "code": code, "type": "Entrada", "qty": qty, "event": "", "ref": sup, "cost": c})
                purchases.append({"date": d, "supplier": sup, "lines": lines})
            for code, i in ITEM.items():                       # waste on perishables
                if i["cat"] in WASTE and stock[code] > 0:
                    q = round(usage_week[code] * rng.uniform(*WASTE[i["cat"]]), 1)
                    q = min(q, stock[code])
                    if q > 0:
                        stock[code] = round(stock[code] - q, 1)
                        c = unit_cost(code, d)
                        moves.append({"date": d, "code": code, "type": "Merma", "qty": q, "event": "", "ref": "Merma semanal", "cost": c})
                        waste_cost[(d.year, d.month)] += q * c
        for m in by_day.get(d, []):
            stock[m["code"]] = round(stock[m["code"]] - m["qty"], 1)
        if d.day == 1 and d > START:                           # monthly physical count adjustment
            for code in rng.sample(list(ITEM), 4):
                q = round(usage_week[code] * rng.uniform(-0.08, 0.05), 1)
                if q:
                    stock[code] = round(stock[code] + q, 1)
                    moves.append({"date": d, "code": code, "type": "Ajuste", "qty": q, "event": "", "ref": "Conteo físico", "cost": unit_cost(code, d)})
    # the October 3 wedding season ate into ice and shrimp; the late deliveries leave them under minimum
    for code in stock:
        stock[code] = max(0.0, stock[code])

    # profit and loss per quincena (operating profit before income tax; propina legal passes to staff)
    q = defaultdict(lambda: {"rev": 0.0, "food": 0.0, "staff": 0.0, "other": 0.0, "fixed": 0.0, "waste": 0.0})
    qkey = lambda d: (d.year, d.month, 1 if d.day <= 15 else 2)
    fixed_payroll = sum(s for _, _, s in FIXED_STAFF) * EMPLOYER_CHARGES * overhead_k
    overhead = sum(v[1] for v in FIXED_OVERHEAD.values()) * overhead_k
    m = date(START.year, START.month, 1)
    while m <= END:
        for half in (1, 2):
            q[(m.year, m.month, half)]["fixed"] += (fixed_payroll + overhead) / 2
            q[(m.year, m.month, half)]["waste"] += waste_cost[(m.year, m.month)] / 2
        m = date(m.year + m.month // 12, m.month % 12 + 1, 1)
    for e in events:
        if e["status"] == "Realizado" and e["date"] <= END:
            k = qkey(e["date"])
            q[k]["rev"] += e["sub"]; q[k]["food"] += e["food"]; q[k]["staff"] += e["staff_cost"]; q[k]["other"] += e["other"]
    for k, v in q.items():
        v["profit"] = v["rev"] - v["food"] - v["staff"] - v["other"] - v["fixed"] - v["waste"]
        v["profit_usd"] = v["profit"] / rate_sell(date(k[0], k[1], 15 if k[2] == 1 else 28))
    rev = sum(v["rev"] for v in q.values())
    profit = sum(v["profit"] for v in q.values())
    res = {"events": events, "moves": moves, "purchases": purchases, "stock": stock, "minimum": minimum,
           "quincenas": q, "revenue": rev, "profit": profit, "margin": profit / rev,
           "avg_profit_usd": sum(v["profit_usd"] for v in q.values()) / len(q),
           "fixed_payroll": fixed_payroll, "overhead": overhead, "unit_cost": unit_cost}
    return res


def calibrate():
    """Event volume sets profit at US$5,000 a quincena; price level then holds the margin at 30%."""
    best = None
    for vol in [0.55 + 0.05 * i for i in range(14)]:
        pk = 1.0
        for _ in range(8):                                    # price for a 30% margin at this volume, within ±25% of market
            s = simulate(pk, vol)
            pk = min(1.25, max(0.80, pk * (1 + (TARGET_MARGIN - s["margin"]) * 0.9)))
        s = simulate(pk, vol)
        gap = abs(s["avg_profit_usd"] - GOAL_USD_QUINCENA) + 20000 * abs(s["margin"] - TARGET_MARGIN)
        if best is None or gap < best[0]:
            best = (gap, pk, vol, s)
    _, pk, vol, s = best
    return pk, vol, s


# ---------- documents: sales, purchases, collections, bank ----------

def write_csv(name, header, rows):
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / name, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def fmt(d):
    return d.isoformat() if d else ""


def build_documents(s, pk, ok):
    events = s["events"]
    seq = {"B01": 201, "B02": 1101, "B04": 11, "B11": 41}
    nxt = lambda t: f"{t}{seq.__setitem__(t, seq[t] + 1) or seq[t] - 1:08d}"
    sales, collections = [], []
    for e in events:
        if e["status"] != "Realizado":
            continue
        c = e["client"]
        delay = rng.randint(0, 3) if rng.random() > 0.12 else rng.randint(7, 21)     # late invoicing is a process gap
        inv_date = e["date"] + timedelta(days=delay)
        if inv_date > TODAY:
            inv_date = TODAY
        t = "B01" if c["type"] != "Particular" else "B02"
        ncf = nxt(t)
        cur = "USD" if e["usd"] else "DOP"
        rate = rate_sell(inv_date) if cur == "USD" else 1.0
        sub = r2(e["sub"] / rate) if cur == "USD" else e["sub"]
        itb, tip = r2(sub * ITBIS), r2(sub * PROPINA)
        total = r2(sub + itb + tip)
        credit = c["type"] != "Particular"
        due = inv_date + timedelta(days=c["days"]) if credit else inv_date
        sales.append({"date": inv_date, "ncf": ncf, "type": t, "rnc": c["rnc"], "client": c["name"], "event": e["id"],
                      "sub": sub, "itbis": itb, "tip": tip, "total": total, "cur": cur, "rate": rate,
                      "total_dop": r2(total * rate), "cond": "Credito" if credit else "Contado",
                      "days": c["days"] if credit else 0, "due": due, "mod": ""})
        e["invoice"] = ncf
        e["invoice_date"] = inv_date
        total_dop = r2(total * rate)
        dep = r2(total_dop * 0.5) if e["deposit_date"] else 0.0
        e["deposit"] = dep
        if dep:
            collections.append({"date": e["deposit_date"], "ncf": ncf, "rnc": c["rnc"], "amount": dep, "how": "Transferencia",
                                "kind": "Anticipo"})
        rest = r2(total_dop - dep)
        if not credit:
            collections.append({"date": e["date"], "ncf": ncf, "rnc": c["rnc"], "amount": rest,
                                "how": rng.choice(["Transferencia", "Transferencia", "Tarjeta", "Efectivo"]), "kind": "Saldo"})
        else:
            lo, hi = c["late"]
            pay = due + timedelta(days=rng.randint(lo, hi))
            if pay <= TODAY:
                collections.append({"date": pay, "ncf": ncf, "rnc": c["rnc"], "amount": rest,
                                    "how": rng.choice(["Transferencia", "Transferencia", "Cheque"]), "kind": "Saldo"})
            elif c["late"][0] < 30 and rng.random() < 0.15 and due - timedelta(days=5) <= TODAY:
                collections.append({"date": TODAY - timedelta(days=rng.randint(0, 4)), "ncf": ncf, "rnc": c["rnc"],
                                    "amount": r2(rest * 0.5), "how": "Transferencia", "kind": "Abono"})
    # credit notes: fewer guests than invoiced
    for e in rng.sample([e for e in events if e.get("invoice") and e["kind"] in ("Corporativo", "Concierto")], 5):
        orig = next(x for x in sales if x["ncf"] == e["invoice"])
        f = rng.uniform(0.05, 0.12)
        sub = r2(orig["sub"] * f); itb, tip = r2(sub * ITBIS), r2(sub * PROPINA)
        d = min(TODAY, orig["date"] + timedelta(days=rng.randint(2, 10)))
        sales.append({**orig, "date": d, "ncf": nxt("B04"), "type": "B04", "sub": sub, "itbis": itb, "tip": tip,
                      "total": r2(sub + itb + tip), "total_dop": r2((sub + itb + tip) * orig["rate"]), "cond": "Contado",
                      "days": 0, "due": d, "mod": orig["ncf"]})
    sales.sort(key=lambda x: (x["date"], x["ncf"]))

    # purchases: inventory orders + monthly overhead + per-event rentals and transport
    purchases = []
    def add_purchase(d, sup_name, cat, sub, itb, other=0.0, ncf=None):
        sup = SUPPLIERS[sup_name]
        if ncf is None:
            if sup["informal"]:
                ncf = nxt("B11")
            elif sup["ecf"]:
                seq.setdefault(sup_name, rng.randint(100, 9000))
                seq[sup_name] += 1
                ncf = f"E31{seq[sup_name]:010d}"
            else:
                seq.setdefault(sup_name, rng.randint(100, 9000))
                seq[sup_name] += 1
                ncf = f"B01{seq[sup_name]:08d}"
        total = r2(sub + itb + other)
        due = d + timedelta(days=sup["days"])
        purchases.append({"date": d, "ncf": ncf, "type": ncf[:3], "rnc": sup["rnc"], "supplier": sup_name, "cat": cat,
                          "sub": r2(sub), "itbis": r2(itb), "other": r2(other), "total": total, "cur": "DOP", "rate": 1.0,
                          "total_dop": total, "cond": "Credito" if sup["days"] else "Contado", "due": due})
    for p in s["purchases"]:
        sub = sum(q * c for _, q, c in p["lines"])
        itb = sum(q * c * ITEM[code]["itbis"] for code, q, c in p["lines"])
        add_purchase(p["date"], p["supplier"], SUPPLIERS[p["supplier"]]["cat"], sub, itb)
        p["ncf"] = purchases[-1]["ncf"]
    m = date(START.year, START.month, 1)
    while m <= date(TODAY.year, TODAY.month, 1):
        for cat, (sup, base, rate, other) in FIXED_OVERHEAD.items():
            d = m + timedelta(days={"Alquiler local": 0, "Electricidad": 9, "Telecomunicaciones": 4, "Mantenimiento": 19}[cat])
            if d > TODAY:
                continue
            sub = base * (rng.uniform(0.9, 1.15) if cat == "Electricidad" else 1)
            add_purchase(d, sup, cat, sub, sub * rate, sub * other)
        m = date(m.year + m.month // 12, m.month % 12 + 1, 1)
    for e in events:
        if e["status"] == "Realizado":
            rent, transp = e["other"] * 0.6, e["other"] * 0.4
            add_purchase(e["date"], "Alquileres Eventos Premium SRL", "Alquiler mobiliario", rent, rent * ITBIS)
            add_purchase(e["date"], "Transporte Express Ozama SRL", "Transporte", transp, transp * ITBIS)
    purchases.sort(key=lambda x: (x["date"], x["ncf"]))
    return sales, collections, purchases, seq


def inject_errors(sales, purchases):
    """Mistakes a real month contains, so the validation rules have something to catch (September 2026)."""
    sep = lambda rows: [r for r in rows if r["date"].year == 2026 and r["date"].month == 9]
    notes = []
    p = next(r for r in sep(purchases) if r["supplier"] == "Bebidas del Caribe SA")
    p["total"] = r2(p["total"] + 900); p["total_dop"] = p["total"]
    notes.append(f"{p['ncf']} ({p['supplier']}): total typed RD$900 higher than subtotal + ITBIS")
    dup = next(r for r in sep(purchases) if r["supplier"] == "Carnes Selectas Quisqueya SRL")
    purchases.append(dict(dup)); notes.append(f"{dup['ncf']} ({dup['supplier']}): entered twice")
    miss = next(r for r in sep(purchases) if r["supplier"] == "Servicios Técnicos de Cocina SRL")
    miss["rnc"] = ""; notes.append(f"{miss['ncf']} ({miss['supplier']}): supplier RNC left blank")
    fut = next(r for r in sep(purchases) if r["supplier"] == "Desechables Santo Domingo SRL")
    fut["export_date"] = fut["date"].replace(month=11)
    notes.append(f"{fut['ncf']} ({fut['supplier']}): date typed as November instead of September")
    s = next(r for r in sep(sales) if r["type"] == "B01" and r["cur"] == "DOP")
    s["total_export"] = r2(s["sub"] + s["itbis"])
    notes.append(f"{s['ncf']} ({s['client']}): propina legal left out of the total")
    purchases.sort(key=lambda x: (x["date"], x["ncf"]))
    return notes


# ---------- e-CF samples (synthetic, for check-ecf until the real files arrive) ----------

def ecf_xml(encf, issuer, issuer_name, buyer, d, gravado18, gravado16=0.0, exento=0.0, ref=None, usd=None):
    itb = r2(gravado18 * 0.18 + gravado16 * 0.16)
    total = r2(gravado18 + gravado16 + exento + itb)
    parts = [f'<?xml version="1.0" encoding="utf-8"?>\n<!-- SINTÉTICO: generado por pilot/generate.py, no es un documento real -->\n<ECF>\n  <Encabezado>\n    <IdDoc>\n      <TipoeCF>{encf[1:3]}</TipoeCF>\n      <eNCF>{encf}</eNCF>\n    </IdDoc>',
             f'    <Emisor>\n      <RNCEmisor>{issuer}</RNCEmisor>\n      <RazonSocialEmisor>{escape(issuer_name)}</RazonSocialEmisor>\n      <FechaEmision>{d.strftime("%d-%m-%Y")}</FechaEmision>\n    </Emisor>',
             f'    <Comprador>\n      <RNCComprador>{buyer}</RNCComprador>\n    </Comprador>',
             f'    <Totales>\n      <MontoGravadoTotal>{gravado18 + gravado16:.2f}</MontoGravadoTotal>\n      <MontoGravadoI1>{gravado18:.2f}</MontoGravadoI1>\n      <MontoGravadoI2>{gravado16:.2f}</MontoGravadoI2>\n      <MontoExento>{exento:.2f}</MontoExento>\n      <TotalITBIS>{itb:.2f}</TotalITBIS>\n      <MontoTotal>{total:.2f}</MontoTotal>\n    </Totales>']
    if usd:
        parts.append(f'    <OtraMoneda>\n      <TipoMoneda>USD</TipoMoneda>\n      <TipoCambio>{usd:.4f}</TipoCambio>\n      <MontoTotalOtraMoneda>{total / usd:.2f}</MontoTotalOtraMoneda>\n    </OtraMoneda>')
    parts.append("  </Encabezado>")
    if ref:
        parts.append(f"  <InformacionReferencia>\n    <NCFModificado>{ref}</NCFModificado>\n  </InformacionReferencia>")
    parts.append("</ECF>\n")
    return "\n".join(parts)


def write_ecf_samples():
    d = OUT / "ecf"
    d.mkdir(parents=True, exist_ok=True)
    bev, avi = SUPPLIERS["Bebidas del Caribe SA"], SUPPLIERS["Avícola del Cibao SRL"]
    tele = next(c for c in CLIENTS if c["name"] == "Telecomunicaciones del Caribe SA")
    other_company = new_rnc("1")
    files = {
        "compra_E310000004521.xml": ecf_xml("E310000004521", bev["rnc"], bev["name"], OWN_RNC, date(2026, 9, 21), 18_450.00),
        "venta_prueba_E310000000001.xml": ecf_xml("E310000000001", OWN_RNC, COMPANY["name"], tele["rnc"], date(2026, 10, 2), 240_000.00),
        "nota_credito_E340000000117.xml": ecf_xml("E340000000117", avi["rnc"], avi["name"], OWN_RNC, date(2026, 9, 25), 0, exento=3_240.00, ref="E310000003302"),
        "compra_usd_E310000000890.xml": ecf_xml("E310000000890", "130998877" if rnc_checksum_ok("130998877") else new_rnc("1"),
                                                "Equipos Gastronómicos del Caribe SRL", OWN_RNC, date(2026, 9, 10),
                                                r2(4_850 * 61.2850), usd=61.2850),
        "otra_empresa_E310000004533.xml": ecf_xml("E310000004533", bev["rnc"], bev["name"], other_company, date(2026, 9, 22), 9_800.00),
    }
    for name, xml in files.items():
        (d / name).write_text(xml, encoding="utf-8")
    return list(files)


# ---------- bank statement, September 2026 ----------

def bank_statement(sales, collections, purchases, events, s):
    rows, bal = [], 1_850_000.00
    sep = lambda d: d.year == 2026 and d.month == 9
    lines = []
    for c in collections:
        if sep(c["date"]):
            lines.append((c["date"], f"{'Depósito' if c['how'] != 'Transferencia' else 'Transferencia recibida'} {c['kind'].lower()} {c['ncf']}", c["ncf"], 0.0, c["amount"]))
    for p in purchases:
        if sep(p["due"]) and p["rnc"] != "":
            lines.append((p["due"], f"Pago a {p['supplier']}", p["ncf"], p["total_dop"], 0.0))
    for e in events:
        if e["status"] == "Realizado" and sep(e["date"] + timedelta(days=1)):
            lines.append((e["date"] + timedelta(days=1), f"Pago personal por evento {e['id']}", e["id"], e["staff_cost"], 0.0))
    half = s["fixed_payroll"] / EMPLOYER_CHARGES / 2
    lines += [(date(2026, 9, 15), "Nómina quincenal personal fijo", "NOM-0915", r2(half * 0.9409), 0.0),
              (date(2026, 9, 30), "Nómina quincenal personal fijo", "NOM-0930", r2(half * 0.9409), 0.0),
              (date(2026, 9, 3), "TSS: seguridad social agosto", "TSS-0826", r2(s["fixed_payroll"] / EMPLOYER_CHARGES * 0.2246), 0.0)]
    aug = lambda r: r["date"].year == 2026 and r["date"].month == 8
    itbis_due = sum(r["itbis"] * r["rate"] * (-1 if r["type"] == "B04" else 1) for r in sales if aug(r)) - sum(r["itbis"] for r in purchases if aug(r))
    lines.append((date(2026, 9, 18), "DGII: ITBIS (IT-1) agosto", "IT1-0826", r2(max(0, itbis_due)), 0.0))
    tips_aug = sum(r["tip"] * r["rate"] for r in sales if aug(r) and r["type"] != "B04")
    lines.append((date(2026, 9, 5), "Distribución propina legal agosto", "PROP-0826", r2(tips_aug), 0.0))
    lines.append((date(2026, 9, 30), "Comisión mantenimiento cuenta", "COM-0930", 350.0, 0.0))
    for d, desc, ref, deb, cre in sorted(lines, key=lambda x: (x[0], -x[4])):
        bal = r2(bal - deb + cre)
        rows.append([fmt(d), desc, ref, f"{deb:.2f}" if deb else "", f"{cre:.2f}" if cre else "", f"{bal:.2f}"])
    write_csv("banco_2026-09.csv", ["Fecha", "Descripcion", "Referencia", "Debito", "Credito", "Balance"], rows)


# ---------- main ----------

def main():
    pk, ok, s = calibrate()
    rng.seed(3)
    sales, collections, purchases, seq = build_documents(s, pk, ok)
    errors = inject_errors(sales, purchases)
    events = s["events"]

    write_csv("ventas.csv", ["Fecha", "NCF", "Tipo_NCF", "NCF_Modificado", "RNC_Cliente", "Cliente", "Evento_ID", "Subtotal",
                             "ITBIS", "Propina_Legal", "Total", "Moneda", "Tasa", "Total_DOP", "Condicion", "Dias_Credito", "Vence"],
              [[fmt(r["date"]), r["ncf"], r["type"], r["mod"], r["rnc"], r["client"], r["event"]]
               + [f"{v * (-1 if r['type'] == 'B04' else 1):.2f}" for v in (r["sub"], r["itbis"], r["tip"], r.get("total_export", r["total"]))]
               + [r["cur"], f"{r['rate']:.4f}", f"{r['total_dop'] * (-1 if r['type'] == 'B04' else 1):.2f}", r["cond"], r["days"],
                  fmt(r["due"])] for r in sales])   # credit notes negative, as the Power BI model expects
    write_csv("compras.csv", ["Fecha", "NCF", "Tipo_NCF", "RNC_Suplidor", "Suplidor", "Categoria", "Subtotal", "ITBIS",
                              "Otros_Impuestos", "Total", "Moneda", "Tasa", "Total_DOP", "Condicion", "Vence"],
              [[fmt(r.get("export_date", r["date"])), r["ncf"], r["type"], r["rnc"], r["supplier"], r["cat"], f"{r['sub']:.2f}",
                f"{r['itbis']:.2f}", f"{r['other']:.2f}", f"{r['total']:.2f}", r["cur"], f"{r['rate']:.4f}", f"{r['total_dop']:.2f}",
                r["cond"], fmt(r["due"])] for r in purchases])
    write_csv("cobros.csv", ["Fecha", "NCF_Factura", "RNC_Cliente", "Monto_DOP", "Forma_Pago", "Tipo_Cobro"],
              [[fmt(c["date"]), c["ncf"], c["rnc"], f"{c['amount']:.2f}", c["how"], c["kind"]] for c in sorted(collections, key=lambda c: c["date"])])
    write_csv("eventos.csv", ["Evento_ID", "Fecha_Evento", "Fecha_Cotizacion", "Fecha_Contrato", "Fecha_Anticipo", "Monto_Anticipo",
                              "RNC_Cliente", "Cliente", "Tipo_Evento", "Invitados", "Ubicacion", "Personal_Por_Evento", "Costo_Alimentos",
                              "Costo_Personal", "Otros_Costos", "Precio_Por_Persona", "NCF_Factura", "Fecha_Factura", "Estado"],
              [[e["id"], fmt(e["date"]), fmt(e["quote"]), fmt(e["contract"]), fmt(e["deposit_date"]), f"{e.get('deposit', 0):.2f}",
                e["client"]["rnc"], e["client"]["name"], e["kind"], e["guests"], e["venue"], e["staff"], f"{e.get('food', 0):.2f}",
                f"{e.get('staff_cost', 0):.2f}", f"{e.get('other', 0):.2f}", e["ppp"], e.get("invoice", ""), fmt(e.get("invoice_date")),
                e["status"]] for e in events])
    write_csv("clientes.csv", ["RNC_Cliente", "Cliente", "Tipo_Cliente", "Sector", "Limite_Credito", "Dias_Credito"],
              [[c["rnc"], c["name"], c["type"], c["sector"], c["limit"], c["days"]] for c in CLIENTS])
    write_csv("suplidores.csv", ["RNC_Suplidor", "Suplidor", "Categoria", "Emite_eCF", "Dias_Credito"],
              [[v["rnc"], k, v["cat"], "Si" if v["ecf"] else "No", v["days"]] for k, v in SUPPLIERS.items()])
    write_csv("inventario_items.csv", ["Codigo", "Producto", "Categoria", "Unidad", "Costo_Unitario", "ITBIS_Tasa", "Stock_Minimo",
                                       "Stock_Actual", "Suplidor_Principal"],
              [[i["code"], i["name"], i["cat"], i["unit"], f"{s['unit_cost'](i['code'], TODAY):.2f}", i["itbis"],
                s["minimum"][i["code"]], s["stock"][i["code"]], SUPPLIERS[i["supplier"]]["rnc"]] for i in ITEM.values()])
    write_csv("inventario_movimientos.csv", ["Fecha", "Codigo", "Tipo_Mov", "Cantidad", "Costo_Unitario", "Evento_ID", "Referencia"],
              [[fmt(m["date"]), m["code"], m["type"], m["qty"], f"{m['cost']:.2f}", m["event"], m["ref"]]
               for m in sorted(s["moves"], key=lambda m: (m["date"], m["code"]))])
    emp = [[f"EMP-{i + 1:03d}", p, a, "Fijo", f"{sal:.2f}", ""] for i, (p, a, sal) in enumerate(FIXED_STAFF)]
    n = len(emp)
    for role, cnt, pay in ROSTER:
        for _ in range(cnt):
            n += 1
            emp.append([f"EMP-{n:03d}", role, "Eventos", "Por evento", "", f"{pay:.2f}"])
    write_csv("empleados.csv", ["Empleado_ID", "Puesto", "Area", "Tipo", "Salario_Mensual", "Pago_Por_Evento"], emp)
    write_csv("tasas.csv", ["Fecha", "Compra", "Venta", "Origen"],
              [[fmt(d), f"{b:.4f}", f"{v:.4f}", "Banco Central (dado por el cliente)" if d == TODAY else "sintética"]
               for d, (b, v) in sorted(RATES.items())])
    ecf_files = write_ecf_samples()
    bank_statement(sales, collections, purchases, events, s)

    # ERP export for the backbone's CSV connector: one month, the columns an ERP export would carry
    exp = OUT / "export_erp"
    exp.mkdir(exist_ok=True)
    with open(exp / "ventas_2026-09.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Fecha", "NCF", "NCF Modificado", "RNC Emisor", "RNC Cliente", "Subtotal", "ITBIS", "Propina", "Total", "Moneda"])
        for r in sales:
            if r["date"].year == 2026 and r["date"].month == 9:
                w.writerow([r["date"].strftime("%d/%m/%Y"), r["ncf"], r["mod"], OWN_RNC, r["rnc"], f"{r['sub']:.2f}",
                            f"{r['itbis']:.2f}", f"{r['tip']:.2f}", f"{r.get('total_export', r['total']):.2f}", r["cur"]])
    with open(exp / "compras_2026-09.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Fecha", "NCF", "RNC Suplidor", "Suplidor", "RNC Comprador", "Subtotal", "ITBIS", "Otros Impuestos", "Total"])
        for r in purchases:
            d = r.get("export_date", r["date"])
            if r["date"].year == 2026 and r["date"].month == 9:
                w.writerow([d.strftime("%d/%m/%Y"), r["ncf"], r["rnc"], r["supplier"], OWN_RNC, f"{r['sub']:.2f}",
                            f"{r['itbis']:.2f}", f"{r['other']:.2f}", f"{r['total']:.2f}"])

    q = s["quincenas"]
    summary = {
        "company": COMPANY, "period": [fmt(START), fmt(END)], "today": fmt(TODAY), "rate_today": RATE_TODAY,
        "revenue_dop_12m": round(s["revenue"]), "profit_dop_12m": round(s["profit"]), "margin": round(s["margin"], 4),
        "avg_profit_usd_quincena": round(s["avg_profit_usd"]),
        "quincenas_below_goal": sum(1 for v in q.values() if v["profit_usd"] < GOAL_USD_QUINCENA), "quincenas": len(q),
        "events_done": sum(1 for e in events if e["status"] == "Realizado"),
        "events_booked": sum(1 for e in events if e["status"] != "Realizado"),
        "sales_docs": len(sales), "purchase_docs": len(purchases), "inventory_moves": len(s["moves"]),
        "calibration": {"price_level": round(pk, 4), "event_volume": round(ok, 4)},
        "injected_errors_sep_2026": errors, "ecf_samples": ecf_files,
    }
    (OUT / "resumen.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k not in ("company",)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
