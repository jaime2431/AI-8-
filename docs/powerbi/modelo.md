# Modelo Power BI — Sabor & Escena Catering SRL (piloto Datia)

Versión 1.0 · 7-oct-2026 · Medidas en `medidas.dax` · Pasos de armado en `README.md`

Empresa ficticia de catering de eventos (corporativos, conciertos, bodas, sociales). Unos 100 colaboradores
(12 fijos + ~88 por evento). Ventas ≈ RD$2.05 MM/mes antes de ITBIS, margen neto objetivo 30 %.
Meta del dueño: **US$5,000 de beneficio por quincena** (≈ RD$307,056 a la tasa de venta 61.4111).

Reglas que no cambian:

- Moneda del modelo: **RD$**. Facturas en USD se convierten con la `Tasa` de la factura (tasa de **venta**
  del Banco Central, política de la empresa). Las medidas en US$ usan la tasa de venta del día (`Fecha[Tasa_Venta]`).
- **Ventas = Subtotal sin ITBIS y sin propina.** La propina legal (10 %) es de los empleados: se informa,
  no es ingreso. El ITBIS se informa aparte.
- Los montos de facturas vienen de `clean_invoices` (la base limpia). Lo que no pasó la validación
  no suma en ventas ni compras: aparece en la tabla `fValidacion`.
- Ninguna relación bidireccional. Ninguna relación muchos-a-muchos.

---

## 1. Tablas

### Hechos

| Tabla | Grano | Origen | Columnas clave (tipo) |
|---|---|---|---|
| `fVentas` | 1 fila por factura de venta (NCF) | `clean_invoices` (direction = `sale`) + columnas extra de `ventas.csv` unidas por `NCF` | Fecha (fecha), NCF (texto, único), Tipo_NCF (texto 3), RNC_Cliente (texto), Evento_ID (texto), Moneda, Tasa (dec 4), Subtotal, ITBIS, Propina_Legal, Total (dec 2, moneda original), **Subtotal_DOP, ITBIS_DOP, Propina_DOP, Total_DOP** (dec 2, = monto × Tasa), Condicion, Dias_Credito (entero), Vence (fecha), **Secuencial** (entero, parte numérica del NCF) |
| `fCompras` | 1 fila por factura de compra (RNC_Suplidor + NCF) | `clean_invoices` (direction = `purchase`) + `compras.csv` unidas por RNC_Suplidor + NCF | Fecha, NCF, Tipo_NCF, RNC_Suplidor, Categoria, Subtotal_DOP, ITBIS_DOP, Total_DOP, Condicion, Vence |
| `fCobros` | 1 fila por pago recibido | `cobros.csv` | Fecha, NCF_Factura, RNC_Cliente, Monto_DOP, Forma_Pago |
| `fEventos` | 1 fila por evento | `eventos.csv` | Evento_ID, Fecha_Evento, Fecha_Cotizacion, Fecha_Contrato, Fecha_Anticipo, Monto_Anticipo, RNC_Cliente, Invitados, Personal_Por_Evento, Costo_Alimentos, Costo_Personal, Otros_Costos, Precio_Por_Persona, Estado |
| `fMovInventario` | 1 fila por movimiento | `inventario_movimientos.csv` | Fecha, Codigo, Tipo_Mov, Cantidad (dec), Evento_ID, Referencia |
| `fValidacion` | 1 fila por documento con problema | Power Query: filas de `ventas.csv`/`compras.csv` cuyo NCF **no** está en `clean_invoices`, más reglas de validación replicadas | Fecha, NCF, Tipo_NCF, Direccion (Venta/Compra), RNC, Total_DOP, Motivo |

`Subtotal_DOP = Subtotal × Tasa`, etc. (`Tasa = 1` en RD$). `Secuencial = Number.From(Text.End(NCF, si empieza con "E" 10 si no 8))`.

Motivos de `fValidacion` (mismas reglas que `backbone/validation.py`): *En revisión (no está en BD limpia)*,
*Formato NCF/e-NCF inválido*, *ITBIS fuera de 0–18 % del subtotal*, *Subtotal+ITBIS+Propina ≠ Total*,
*Falta RNC del comprador en B01/E31*, *NCF duplicado*, *USD sin tasa del Banco Central*.

### Dimensiones

| Tabla | Clave | Origen | Columnas |
|---|---|---|---|
| `Fecha` (calendario) | Fecha | Power Query (ver §3) | ver §3. Se llama `Fecha` porque las plantillas de control de calidad del backbone usan `'Fecha'[Fecha]` |
| `dCliente` | RNC_Cliente | `clientes.csv` + RNC que aparezcan en ventas sin ficha ("Cliente no registrado") | Cliente, Tipo_Cliente, Sector, Limite_Credito, Dias_Credito |
| `dSuplidor` | RNC_Suplidor | `suplidores.csv` + RNC de compras sin ficha | Suplidor, Categoria, Emite_eCF |
| `dEvento` | Evento_ID | `eventos.csv` (solo atributos) | Tipo_Evento, Cliente, Ubicacion, Estado, Fecha_Evento (atributo, **sin** relación con Fecha) |
| `dProducto` | Codigo | `inventario_items.csv` (foto actual del stock) | Producto, Categoria, Unidad, Costo_Unitario, Stock_Minimo, Stock_Actual, Suplidor_Principal |
| `dTipoNCF` | Tipo_NCF | Tabla manual (Contabilidad la mantiene) | Descripcion, Es_eCF (V/F), Da_Credito_Fiscal (V/F), Sec_Desde, Sec_Hasta, Sec_Vence (secuencia autorizada vigente por tipo) |
| `dCategoriaCompra` | Categoria | Tabla manual | Grupo: `Inventario (costo vía eventos)` · `Costo de evento (ya en eventos)` · `Gasto operativo` · `Activo` |
| `dEmpleado` | Empleado_ID | `empleados.csv` | Puesto, Area, Tipo, Salario_Mensual, Pago_Por_Evento. **Desconectada** (solo para nómina fija) |
| `_Medidas` | — | tabla vacía | contiene todas las medidas |

`dTipoNCF` inicial: B01, B02, B04, B14, B15, B11, B13 (Es_eCF = F) y E31, E32, E33, E34, E41, E43, E44, E45
(Es_eCF = V). Da_Credito_Fiscal = V para B01, B04, B11, B13*, E31, E33, E34, E41 (*Contabilidad confirma).

`dCategoriaCompra` evita contar dos veces: alimentos y bebidas comprados entran a inventario y llegan al
resultado por `fEventos[Costo_Alimentos]`; transporte y alquiler de equipos del evento ya están en
`Otros_Costos`. Solo `Gasto operativo` (alquiler del local, luz, agua, gas de cocina, telecom, mantenimiento,
mercadeo, honorarios) se resta en el beneficio neto.

---

## 2. Relaciones

Todas: muchos-a-uno (*:1), filtro **simple** (de la dimensión hacia el hecho).

| Desde (muchos) | Hacia (uno) | Activa | Uso |
|---|---|---|---|
| fVentas[Fecha] | Fecha[Fecha] | Sí | ventas por fecha de factura |
| fVentas[Vence] | Fecha[Fecha] | **No** | vencimientos (USERELATIONSHIP) |
| fVentas[RNC_Cliente] | dCliente[RNC_Cliente] | Sí | |
| fVentas[Evento_ID] | dEvento[Evento_ID] | Sí | facturas del evento |
| fVentas[Tipo_NCF] | dTipoNCF[Tipo_NCF] | Sí | e-CF |
| fCompras[Fecha] | Fecha[Fecha] | Sí | |
| fCompras[RNC_Suplidor] | dSuplidor[RNC_Suplidor] | Sí | |
| fCompras[Categoria] | dCategoriaCompra[Categoria] | Sí | |
| fCompras[Tipo_NCF] | dTipoNCF[Tipo_NCF] | Sí | |
| fCobros[Fecha] | Fecha[Fecha] | Sí | cobros por fecha de pago |
| fCobros[RNC_Cliente] | dCliente[RNC_Cliente] | Sí | |
| fEventos[Fecha_Evento] | Fecha[Fecha] | Sí | eventos por fecha del evento |
| fEventos[Fecha_Cotizacion] | Fecha[Fecha] | **No** | embudo por fecha de cotización |
| fEventos[Evento_ID] | dEvento[Evento_ID] | Sí | (1:1 en la práctica; se declara *:1 simple) |
| fEventos[RNC_Cliente] | dCliente[RNC_Cliente] | Sí | |
| fMovInventario[Fecha] | Fecha[Fecha] | Sí | |
| fMovInventario[Codigo] | dProducto[Codigo] | Sí | |
| fMovInventario[Evento_ID] | dEvento[Evento_ID] | Sí | consumo por evento |
| dProducto[Suplidor_Principal] | dSuplidor[RNC_Suplidor] | Sí | copo de nieve; suplidor de reorden |
| fValidacion[Fecha] | Fecha[Fecha] | Sí | |
| fValidacion[Tipo_NCF] | dTipoNCF[Tipo_NCF] | Sí | |

Sin relación a propósito:

- **fCobros ↔ fVentas**: se cruzan en las medidas por `NCF_Factura = NCF` (filtro explícito). Una relación
  hecho-hecho crearía caminos ambiguos hacia `Fecha` y `dCliente`.
- **dEvento ↔ Fecha**: `fEventos` lleva la fecha; si `dEvento` también la llevara habría dos caminos.
- **dEmpleado**: la nómina fija se prorratea por día en la medida.

Diagrama (simplificado):

```
           dTipoNCF ─┬─────────────┬──────────── fValidacion ── Fecha
                     │             │
dCliente ── fVentas ─┘   fCompras ─┴── dSuplidor ── dProducto ── fMovInventario
   │   │       │            │                                      │
   │   └ fCobros   dCategoriaCompra                     dEvento ───┘
   └──── fEventos ─────────────────────────────────────── dEvento
 (Fecha filtra fVentas, fCompras, fCobros, fEventos, fMovInventario, fValidacion)
```

---

## 3. Calendario `Fecha`

Construido en Power Query (no con CALENDARAUTO) para poder unir tasas y feriados. Marcar como tabla de fechas.

- Rango: 1-ene del primer año con datos hasta 31-dic del año actual (zona RD, UTC−4).
- Columnas: `Fecha`, `Año`, `Trimestre` ("T1".."T4"), `Mes_Num`, `Mes` ("ene".."dic", ordenar por Mes_Num),
  `Año_Mes` ("2026-10"), `Dia`, `Dia_Semana` ("lun".."dom"), **`Quincena_Num`** (1 si día ≤ 15, si no 2),
  **`Quincena_ID`** ("2026-10-Q1"), **`Quincena_Etiqueta`** ("1–15 oct 2026" / "16–31 oct 2026"),
  `Quincena_Orden` (Año×1000 + Mes×10 + Quincena_Num; ordena Quincena_ID y Quincena_Etiqueta),
  `Quincena_Inicio`, `Quincena_Fin`, `Es_Feriado`, `Nombre_Feriado`, `Es_Laborable` (lun–sáb y no feriado),
  `Tasa_Compra`, `Tasa_Venta` (de `tasas.csv`, rellenadas hacia abajo para fines de semana, feriados y
  días futuros con la última tasa conocida).
- **Jerarquía** `Calendario`: Año › Trimestre › Mes › Quincena_Etiqueta › Fecha.
- **Feriados RD**: tabla manual `Feriados` (Fecha, Nombre) unida en Power Query. La Ley 139-97 traslada
  algunos feriados al lunes; cargar cada año la lista oficial del Ministerio de Trabajo. Referencia 2026
  (verificar traslados): 1-ene Año Nuevo; 5-ene Reyes (trasladado); 21-ene Altagracia; 26-ene Duarte;
  27-feb Independencia; 3-abr Viernes Santo; 4-may Trabajo (trasladado); 4-jun Corpus Christi;
  16-ago Restauración; 24-sep Mercedes; 9-nov Constitución (trasladado); 25-dic Navidad.
  Los feriados importan: conciertos y bodas se concentran ahí, y la cocina compra el día laborable anterior.

Otras jerarquías: `dEvento` Tipo_Evento › Evento_ID · `dCliente` Tipo_Cliente › Sector › Cliente ·
`dProducto` Categoria › Producto.

---

## 4. Actualización incremental

Parámetros `RangeStart` / `RangeEnd` (fecha/hora). Filtro en Power Query:
`each [Fecha] >= Date.From(RangeStart) and [Fecha] < Date.From(RangeEnd)`.

| Tabla | Archivar | Actualizar | Detectar cambios |
|---|---|---|---|
| fVentas | 3 años | últimos 3 meses (notas de crédito y correcciones llegan tarde) | MAX(`clean_invoices.loaded_at`) |
| fCompras | 3 años | últimos 3 meses | MAX(`loaded_at`) |
| fCobros | 3 años | últimos 3 meses | — |
| fMovInventario | 2 años | últimos 2 meses | — |
| fEventos, fValidacion, dimensiones | completa en cada actualización (pocas filas; las fechas de eventos cambian) | | |

- Mientras la fuente sea CSV la consulta no se pliega (lee el archivo completo); la política funciona
  igual y gana velocidad cuando la fuente pase a Azure SQL.
- Saldos y atrasos se calculan en **medidas**, no en Power Query, para que una partición vieja nunca
  quede con un saldo desactualizado.

---

## 5. Seguridad por filas (RLS)

| Rol | Filtros | Ve |
|---|---|---|
| **Dueño** | ninguno | todo |
| **Contabilidad** | ninguno sobre datos financieros; `dEmpleado`: todo | todo; recibe la app sin la página Supervisión si el dueño lo pide |
| **Operaciones** | `fCobros`: `FALSE()` · `fValidacion`: `FALSE()` · `dEmpleado`: `[Tipo] = "Por evento"` · `dCategoriaCompra`: `[Grupo] <> "Gasto operativo"` | eventos, costos de eventos, inventario y compras de cocina; no ve cobros, salarios fijos ni gastos generales |

- Seguridad de objetos (OLS, con Tabular Editor) para Operaciones: ocultar `dCliente[Limite_Credito]` y
  `dEmpleado[Salario_Mensual]`. Las páginas Crédito y Resumen no se publican en la audiencia de Operaciones.
- RLS no aplica a Admin/Miembro/Colaborador del área de trabajo: los usuarios del cliente entran como
  **Visor** por la app. El asistente del portal llama `execute_dax` con `impersonated_user`, así que respeta el rol.
- Probar cada rol con "Ver como" antes de publicar.

---

## 6. Páginas del informe

Segmentadores comunes (sincronizados): Año, Mes, Quincena_Etiqueta, Tipo_Evento. Por defecto: quincena actual.
`[Fecha Corte]` = último día del período elegido, nunca después de hoy: los saldos y el stock se leen a esa fecha.

### 0. Resumen

| Visual | Campos | Medida | Qué decisión apoya |
|---|---|---|---|
| KPI | Eje: Quincena_Etiqueta; Objetivo | Beneficio por Quincena USD vs Meta Quincena USD | ¿Llegamos a los US$5,000 esta quincena? |
| Tarjeta | — | Brecha vs Meta USD, Beneficio Proyectado Quincena RD$ | Cuánto falta y si se llegará al cierre |
| Columnas + línea | Eje: Quincena_Etiqueta (últimas 12) | Beneficio Neto RD$ (columnas), Meta Quincena RD$ (línea) | Tendencia del beneficio contra la meta |
| Tarjetas (fila) | — | Ventas RD$ + Ventas Var %, Margen Neto %, CxC Vencida, Eventos en Alerta, Ítems Bajo Mínimo, Días para fecha límite e-CF | Un número por decisión: dónde mirar primero |
| Barras | Eje: Tipo_Evento | Margen por Evento RD$ | Qué tipo de evento deja más dinero |

### 1. Procedimientos (decisión 1)

| Visual | Campos | Medida | Qué decisión apoya |
|---|---|---|---|
| Embudo | Etapas como medidas | Eventos Cotizados (fecha cotización), Eventos Contratados, Eventos con Anticipo, Eventos Realizados, Eventos Facturados, Eventos Cobrados | Dónde se cae el proceso estándar |
| Tarjetas | — | Ciclo Cotización→Contrato, Ciclo Evento→Factura, Ciclo Factura→Cobro, % Eventos con Anticipo, % Cumplimiento Proceso, Tasa de Conversión | Cumplimiento del proceso en un vistazo |
| Columnas agrupadas | Eje: Tipo_Evento | los tres ciclos | Qué tipo de evento se demora más en cada paso |
| Líneas | Eje: Año_Mes | Ciclo Evento→Factura, Ciclo Factura→Cobro | Si el proceso mejora mes a mes |
| Tabla de excepciones | Evento_ID, Cliente, Fecha_Evento, Tipo_Evento | Paso Incumplido (filtro: no vacío) | A quién llamar hoy para cerrar el paso pendiente |

### 2. e-CF y cumplimiento fiscal (decisión 2)

| Visual | Campos | Medida | Qué decisión apoya |
|---|---|---|---|
| Tarjeta grande | — | Días para fecha límite e-CF | Urgencia: faltan 39 días al 7-oct-2026 |
| Tarjetas | — | % Ventas emitidas como e-CF, % Compras recibidas como e-CF, Facturas con Problemas de Validación | Avance real de la migración |
| Líneas | Eje: Año_Mes | % Ventas emitidas como e-CF, % Compras recibidas como e-CF | Si llegamos a 100 % antes del 15-nov |
| Columnas 100 % apiladas | Eje: Año_Mes; Leyenda: Tipo_NCF | Cantidad Facturas | Qué tipos de comprobante quedan en papel |
| Tabla | Tipo_NCF, Sec_Desde, Sec_Hasta, Sec_Vence | Último Secuencial NCF, % Secuencia NCF Usada, NCF Disponibles, Días para Vencer Secuencia | Pedir secuencias a tiempo / no pedir más B si ya migramos |
| Tabla | Suplidor (filtro Emite_eCF = "No") | Compras RD$, ITBIS Compras (crédito fiscal) | A qué suplidores exigir e-CF o reemplazar |
| Tabla | Fecha, NCF, Direccion, RNC, Motivo | Facturas con Problemas de Validación | Qué corregir antes del cierre fiscal |
| Tarjetas | — | ITBIS Ventas, ITBIS Compras (crédito fiscal), ITBIS a Pagar, Propina Legal RD$ | Monto del IT-1 del mes y propina a repartir |

### 3. Supervisión de eventos (decisión 3)

| Visual | Campos | Medida | Qué decisión apoya |
|---|---|---|---|
| Tarjetas | — | Eventos Realizados, Eventos en Alerta, Ticket por Invitado, Margen Promedio por Evento | Salud general sin estar en cada evento |
| Dispersión | Detalle: Evento_ID; Leyenda: Tipo_Evento; X | Costo Alimentos % (X), Margen por Evento % (Y), Ingreso Evento RD$ (tamaño) | Encontrar eventos que perdieron margen y por qué |
| Matriz | Tipo_Evento › Evento_ID, Cliente, Fecha_Evento | Ingreso Evento RD$, Costo Alimentos %, Costo Personal %, Margen por Evento %, Ticket por Invitado, Alerta Evento (color con Color Margen Evento) | Revisar cada evento con semáforo |
| Líneas | Eje: Año_Mes; líneas constantes 35 % y 25 % | Costo Alimentos %, Costo Personal % | Si el costo de comida o personal se dispara |
| Barras | Eje: Ubicacion | Margen por Evento % | Dónde (salón, playa, estadio) se gana menos |
| Obtener detalles → "Ficha de Evento" | Evento_ID | Ingreso, costos, consumo de inventario (Valor Salidas, Valor Merma), facturas y saldo | Explicar un evento puntual |

### 4. Inventario (decisión 4)

Stock = foto actual de `inventario_items` (no cambia con el segmentador de fecha); merma y consumo sí.

| Visual | Campos | Medida | Qué decisión apoya |
|---|---|---|---|
| Tarjetas | — | Valor Inventario, Ítems Bajo Mínimo, Merma %, Días de Inventario | Capital parado y riesgo de faltante |
| Tabla de reorden | Producto, Categoria, Unidad, Stock_Actual, Stock_Minimo, Suplidor | Consumo Diario 30D (unidades), Cantidad a Reordenar, Valor a Reordenar (filtro > 0) | Orden de compra de hoy |
| Barras | Eje: Producto (Top 10) | Valor Merma | Qué producto se está botando |
| Líneas | Eje: Año_Mes | Merma % | Si la merma baja con los controles |
| Barras | Eje: Categoria | Días de Inventario | Dónde sobra o falta stock |

### 5. Crédito (decisión 5)

| Visual | Campos | Medida | Qué decisión apoya |
|---|---|---|---|
| Tarjetas | — | CxC Total, CxC Vencida, DSO, Clientes que Exceden Límite, Clientes a Suspender | Exposición total de crédito |
| Barras apiladas | Eje: Cliente (Top 10 por CxC) | CxC 0-30, CxC 31-60, CxC 61-90, CxC >90 | A quién cobrar primero |
| Tabla semáforo | Cliente, Tipo_Cliente, Dias_Credito | Límite Actual, Límite Sugerido, CxC Total, Uso de Límite %, CxC Vencida, CxC >90, Días Promedio de Atraso, Excede Límite, Suspender Crédito | Subir, bajar o cortar el crédito de cada cliente |
| Líneas | Eje: Año_Mes | CxC Total, DSO | Si la cobranza mejora |
| Obtener detalles → "Ficha de Cliente" | RNC_Cliente | facturas abiertas con `_Saldo Factura al Corte` (visible solo en esta página), Vence, días vencidos; historial de pagos | Llamada de cobro con datos |

Páginas ocultas: Ficha de Evento, Ficha de Cliente, Tooltip de evento (Ingreso, Margen %, Alerta).

---

## 7. Umbrales de alerta (en las medidas)

Margen por evento < 20 % · Costo alimentos > 35 % · Costo personal > 25 % · Evento realizado sin factura > 3 días ·
Cliente con saldo > 90 días vencido → límite sugerido 0 y Suspender Crédito = 1.
