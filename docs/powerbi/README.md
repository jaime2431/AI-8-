# Power BI — Sabor & Escena Catering SRL (piloto)

| Archivo | Qué es |
|---|---|
| `modelo.md` | Esquema estrella, relaciones, calendario, actualización incremental, RLS y diseño de las 6 páginas |
| `medidas.dax` | 102 medidas en español (incluye 2 auxiliares ocultas), agrupadas por carpeta, con descripción `///` |

## Armar la plantilla `.pbit` (Power BI Desktop)

1. **Parámetros**: crear `pFuente` (texto: `CSV` o `SQL`), `pCarpetaCSV` (ruta a `pilot/data/`),
   `pServidorSQL` / `pBaseSQL`, y `RangeStart` / `RangeEnd` (fecha/hora).
2. **Consultas** (Power Query), una por tabla de `modelo.md` §1. Con `pFuente = SQL`, `fVentas` y `fCompras`
   salen de `clean_invoices` y se les unen por NCF las columnas extra de `ventas.csv` / `compras.csv`.
   Con `CSV` (antes de la primera carga a la base limpia) salen directo del CSV.
   - Tipos: fechas como *Fecha*, montos como *Número decimal fijo*, RNC, NCF, Codigo y Evento_ID como *Texto*.
   - Crear `Subtotal_DOP`, `ITBIS_DOP`, `Propina_DOP`, `Total_DOP` (= monto × Tasa) y `Secuencial`.
   - `fValidacion`: filas de los CSV que no están en `clean_invoices` + reglas (modelo.md §1).
   - `dCliente` / `dSuplidor`: agregar los RNC que aparecen en hechos sin ficha.
   - `dTipoNCF`, `dCategoriaCompra`, `Feriados`: "Escribir datos"; Contabilidad confirma los valores.
   - `Fecha`: calendario en M con tasas rellenadas hacia abajo y feriados (modelo.md §3). Marcar como tabla de fechas.
   - Tabla vacía `_Medidas`.
3. **Relaciones** exactamente como modelo.md §2 (dos inactivas; ninguna bidireccional). Desactivar
   *Fecha/hora automática* en Opciones.
4. **Medidas**: Vista de consulta DAX → pegar `medidas.dax` → *Actualizar modelo con cambios*. Luego, en la
   vista TMDL (o Propiedades), asignar la carpeta de cada medida según el encabezado `Carpeta:` de su
   sección, los formatos (RD$ `#,0`, % `0.0%`, días `0.0`) y ocultar `_Saldo Factura al Corte` y
   `_Límite Sugerido Cliente`. Ocultar columnas clave y montos crudos de los hechos.
5. **Jerarquías** y orden de columnas (Mes por Mes_Num, Quincena por Quincena_Orden).
6. **Roles** Dueño / Contabilidad / Operaciones (modelo.md §5); OLS con Tabular Editor. Probar con *Ver como*.
7. **Páginas** y visuales según modelo.md §6; páginas de detalle Ficha de Evento y Ficha de Cliente.
8. **Actualización incremental** en fVentas, fCompras, fCobros y fMovInventario (modelo.md §4).
9. Archivo → Exportar → **Plantilla de Power BI (.pbit)**. Guardar `pilot/powerbi/sabor-escena.pbit`.

## Antes de publicar (regla de liberación)

Se publica **primero en el área de trabajo de Prueba**. Pasa a Producción solo cuando cada número de la
lista coincide **al peso** con la base limpia (diferencia 0.00; la tolerancia de 0.5 % del control nocturno
es para la vigilancia diaria, no para liberar).

| Número en Power BI | Contra la base limpia |
|---|---|
| `[Total Facturado RD$]` por día y por mes | `SUM(total_dop)` de `clean_invoices` con `direction='sale'` |
| `[Compras Total RD$]` por día y por mes | `SUM(total_dop)` con `direction='purchase'` |
| `[ITBIS Ventas]` por mes | `SUM(itbis * COALESCE(fx_rate,1))` ventas |
| `[Cantidad Facturas]` | `COUNT(*)` ventas |
| `[CxC Total]` a fin de mes | Σ facturas − Σ cobros con fecha ≤ fin de mes (`cobros.csv`) |
| `[Valor Inventario]` | Σ Stock_Actual × Costo_Unitario (`inventario_items.csv`) |
| `[Facturas con Problemas de Validación]` | filas en `review_queue` abiertas + rechazadas del período |

Además: cada rol probado con *Ver como*; ninguna tarjeta en blanco o con error; segmentador en la quincena
actual muestra `[Meta Quincena RD$]` = 5,000 × tasa de venta del día.

**Control nocturno** (`qa_checks` del cliente en `config/clients.json`). Ojo: `[Ventas RD$]` aquí es **sin
ITBIS**, así que se compara el total facturado:

```json
{"name": "ventas_del_dia",  "db_metric": "sales_total_dop",
 "dax": "EVALUATE ROW(\"v\", CALCULATE([Total Facturado RD$], 'Fecha'[Fecha] = {date}))"},
{"name": "compras_del_dia", "db_metric": "purchases_total_dop",
 "dax": "EVALUATE ROW(\"v\", CALCULATE([Compras Total RD$], 'Fecha'[Fecha] = {date}))"},
{"name": "itbis_ventas",    "db_metric": "itbis_sales_dop",
 "dax": "EVALUATE ROW(\"v\", CALCULATE([ITBIS Ventas], 'Fecha'[Fecha] = {date}))"}
```

`model_description` para el asistente del portal: "Hechos fVentas, fCompras, fCobros, fEventos,
fMovInventario; calendario 'Fecha' (Fecha, Año, Mes, Quincena_ID). Medidas: [Ventas RD$] (sin ITBIS),
[Total Facturado RD$], [Beneficio por Quincena USD], [Meta Quincena USD], [Margen por Evento %],
[CxC Total], [CxC >90], [Límite Sugerido], [Valor Inventario], [Ítems Bajo Mínimo], [% Ventas emitidas como e-CF]."
