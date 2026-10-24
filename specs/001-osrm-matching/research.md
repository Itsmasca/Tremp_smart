# Research — Slice `matching` con OSRM

**Feature**: [spec.md](./spec.md) · **Plan**: [plan.md](./plan.md) · **Fecha**: 2026-10-24

Cada decisión tiene el formato: Decisión / Razón / Alternativas consideradas.

---

## R-01 · Servidor HTTP dentro del proceso existente

- **Decisión**: FastAPI + uvicorn arrancado programáticamente (`uvicorn.Server(config).serve()`) dentro del mismo event loop que `run_consumer`, con `asyncio.gather` en `main.py`. Pool `asyncpg`, cliente Redis y `OsrmClient` se crean una vez en el lifespan y se inyectan vía `app.state` + `Depends`.
- **Razón**: El servicio ya es asyncio (asyncpg, redis.asyncio). Un solo proceso evita duplicar pools y respeta lo que ya define `infra/task-def-v2.json` (contenedor único, puerto 8000). FastAPI devuelve 422 por defecto ante errores de validación Pydantic, que es justo lo que pide el contrato.
- **Alternativas**: (a) dos procesos/contenedores (HTTP y consumer) — más aislamiento pero duplica despliegue y no lo pide nadie aún; (b) aiohttp/Starlette puros — menos boilerplate de validación propio con FastAPI.
- **Consecuencias**:
  - Si uvicorn o el consumer terminan con excepción, el `gather` debe cancelar el otro y el proceso salir con código ≠ 0 (que ECS lo reinicie). No dejar un proceso "medio vivo".
  - El pool asyncpg actual (`max_size=5`) es compartido con el consumer: se sube a configurable `DB_POOL_MAX_SIZE` (default 10). Cada `/match` usa **una** conexión (2 consultas cortas).

## R-02 · Endpoint `/health` (no estaba en la spec)

- **Decisión**: Añadir `GET /health` → 200 `{"status":"ok"}` sin tocar DB/OSRM (liveness). Opcional `GET /ready` que verifica DB + OSRM para diagnósticos.
- **Razón**: `infra/task-def-v2.json` ya define un healthCheck contra `http://localhost:8000/health`. Sin ese endpoint ECS mata la tarea en bucle.
- **Hallazgo crítico**: el healthCheck usa `curl`, y la imagen base `python:3.13-slim` **no trae curl** → el chequeo fallaría siempre. Decisión: cambiar el comando a `python -c "import urllib.request,sys; urllib.request.urlopen('http://localhost:8000/health', timeout=3)"` (o instalar curl en el Dockerfile). Preferimos Python para no engordar la imagen.
- **Alternativas**: health contra `/match` (caro y con efectos: emitiría `match.shown`).

## R-03 · Límite de tamaño de matriz OSRM y partición en lotes

- **Decisión**: OSRM (`osrm-routed --max-table-size N`) rechaza un `/table` cuando `num_sources × num_destinations > N²` (error `TooBig`), no por número de coordenadas. Con la estructura `sources=[A₁…Aₙ, P, D]`, `destinations=[P, D, B₁…Bₙ]` → `(n+2)² ≤ N²` ⇔ `n ≤ N − 2`. Con `N = 100` y `MAX_CANDIDATES = 80` **cabe en una sola llamada**. La partición sólo se activa si `n > N − 2`; cada lote lleva `[A_lote, P, D, B_lote]` (P y D en todos), lotes en paralelo.
- **Acción pendiente**: confirmar contra la versión exacta de la imagen OSRM desplegada (prueba rápida en [quickstart.md](./quickstart.md#5-verificar-el-límite-de-tabla-de-osrm)). Si el contenedor usara la regla por número de coordenadas, el código ya lo soporta: el tamaño de lote se calcula con una sola función `max_batch_candidates(N)`; solo cambia la fórmula.
- **Corrección de la spec Kiro**: `.kiro/specs/matching-osrm/design.md §4.3` y T-07/T-17 indican lotes de 49 (regla por coordenadas). Se deben alinear a esta decisión (ver plan → *Seguimiento*).
- **Otros parámetros de `/table`**: `annotations=duration,distance` (por defecto sólo devuelve duraciones; las distancias se guardan como features).
- **Alternativas**: llamar `/route` por candidato (80 llamadas → rompe el presupuesto de 400 ms).

## R-04 · Snap de pickup/dropoff

- **Decisión**: `GET /nearest/v1/driving/{lon},{lat}?number=1` para P y D en paralelo. Se usa `waypoints[0].location` (lon,lat) como punto ajustado y `waypoints[0].distance` (m) se registra como feature/log.
- **Razón**: Lo pide la spec (FR-011) y asegura que la matriz y el trazado parten del mismo punto que se devuelve.
- **Alternativas**: dejar que `/table` haga el snap implícito — los tiempos serían iguales, pero no tendríamos el punto ajustado para devolver.

## R-05 · Trazado top-K

- **Decisión**: Una llamada `GET /route/v1/driving/{A};{P};{D};{B}?overview=full&geometries=geojson&steps=false` por cada uno de los primeros `ROUTE_GEOMETRY_TOP_K` candidatos, en paralelo. `routes[0].geometry` ya es un GeoJSON LineString: se devuelve tal cual.
- **Razón**: Una sola llamada multi-waypoint devuelve el recorrido completo A→P→D→B. **No se necesita `shapely`** (la spec Kiro lo listaba; se elimina la dependencia).
- **Fallo parcial**: si una llamada `/route` falla, ese candidato devuelve `routeGeometry: null` y se loguea; no se convierte en 503 (los tiempos ya fueron calculados).

## R-06 · Query de candidatos y uso de índices espaciales

- **Decisión**: Una sola query SQL que aplica todos los filtros (estado, asientos, ventana, conductor ≠ pasajero, `NOT EXISTS` match activo, distancia a P y D, dirección) y ordena por `corridor_dist`. Para que el índice espacial se use:
  - Pre-filtro sobre **geometry** (usa un GIST en `rides.route`): `ST_DWithin(r.route, :p, :radius_deg)` con `radius_deg = R / (111_320 · cos(lat))` (sobre-estimado, nunca excluye de más).
  - Filtro exacto sobre **geography**: `ST_DWithin(r.route::geography, :p::geography, R)`.
  - Los puntos se construyen una vez en un CTE (`ST_SetSRID(ST_MakePoint(lng, lat), 4326)`).
- **Razón**: `ST_DWithin(route::geography, …)` por sí solo **no usa** un índice GIST sobre la columna geometry; con miles de rides programados el seq scan rompería el p95.
- **Dependencia con crud-ts**: necesita que exista un índice GIST en `rides.route` y uno en `rides.departure_at` (idealmente parcial `WHERE status = 'scheduled'`). Este slice **no lo crea** (C-08); se pide a crud-ts y se verifica con `EXPLAIN` en el quickstart.
- **Estados de match "activos"**: se tratan como activos todos excepto `rejected`, `completed`, `cancelled` (valores tomados de la spec Kiro). Los valores exactos deben confirmarse con el enum de crud-ts; se dejan en config `MATCH_TERMINAL_STATUSES`.
- **Alternativas**: H3 (preparado vía `CandidateSource`, fuera de alcance), consultar matches en query aparte (round-trip extra, sin beneficio).

## R-07 · Cliente HTTP de OSRM

- **Decisión**: `httpx.AsyncClient` único (creado en el lifespan, `aclose()` al apagar) con `base_url=OSRM_URL`, `Timeout(connect=1.0, read=2.0, write=1.0, pool=1.0)`, `Limits(max_connections=50, max_keepalive_connections=20)`. Un reintento ante `httpx.TransportError`/timeout; **no** se reintenta ante respuestas HTTP 4xx ni `code != "Ok"` de negocio.
- **Razón**: El presupuesto total es 400 ms p95; los timeouts de 3 s / 10 s que proponía la spec Kiro (NF-02) dejarían colgado al pasajero mucho más que el presupuesto. Un timeout corto + 1 reintento y luego 503 es mejor para crud-ts.
- **Mapeo de errores**:
  - Transporte/timeout tras reintento, o HTTP 5xx → `OsrmUnavailable` → **503**.
  - `code == "NoSegment"` en `/nearest` (punto sin calle cerca) → lista vacía (no 503).
  - `code == "TooBig"` → bug de partición → 500 + log de error.
  - Celdas `null` en `/table` → descartar candidato (motivo `no_route`).
- **Coordenadas**: los tipos públicos usan `LatLng(lat, lng)`; sólo `client.py` formatea `"{lng:.6f},{lat:.6f}"`.

## R-08 · Publicación de `match.shown`

- **Decisión**: `XADD rides.events * envelope <json>` (mismo campo `envelope` que lee el consumer), lanzado con `asyncio.create_task` **después** de construir la respuesta. Las tareas se guardan en un `set` con `add_done_callback(discard)` para que el GC no las cancele, y la excepción se loguea en el callback. Sin `MAXLEN` desde este servicio (la retención del stream la gestiona quien la gestione hoy).
- **Envelope**: `{id: uuid4, type: "match.shown", version: 1, source: "matching-py", aggregateType: "match_request", aggregateId: requestId, occurredAt, data}`. Ver [contracts/match-shown.event.schema.json](./contracts/match-shown.event.schema.json).
- **Efecto colateral verificado**: el consumer `matching-preferences` lee todo `rides.events`; `_process_message` hace ACK inmediato a tipos sin handler (`app/shared/events/consumer.py`), así que `match.shown` no rompe el slice preferences.
- **Riesgo de escalado**: `REDIS_CONSUMER_NAME=matching-py-1` es fijo en `infra/task-def-v2.json`. Si se escalan tareas para absorber tráfico HTTP, dos tareas compartirían nombre de consumer. Recomendación: derivarlo del task ID de ECS (`ECS_CONTAINER_METADATA_URI_V4`) o hostname. Fuera de alcance de esta feature, se anota como riesgo.

## R-09 · Preferencias del pasajero

- **Decisión**: `SELECT structured FROM user_preferences WHERE user_id = $1` en paralelo con la query de candidatos (dos conexiones del pool, o secuencial en la misma si el pool está bajo). Se valida con `StructuredPreferences.model_validate` importado de `app/features/preferences/schema.py`. Si falla la validación (schema_version distinto, JSON corrupto) → se ignora y se loguea `prefs_invalid`; la búsqueda sigue sin preferencias.
- **Razón**: Las preferencias nunca deben tumbar una búsqueda. Importar sólo el schema (no el handler ni el LLM) respeta C-02/FR-024.

## R-10 · Scoring

- **Decisión**: `HeuristicV1` puro (sin I/O), pesos desde `Settings`. `time_n` usa `abs(Δ).total_seconds() / (timeWindowMin·60)`; si `timeWindowMin == 0` → `time_n = 0`. Al arrancar se valida que `SCORE_W_* ≥ 0` y suman > 0 (si no suman 1 se normalizan y se avisa en log) para garantizar `score ∈ [0,1]`.
- **Features registradas** por candidato (para `match.shown`): `detour_s_raw`, `detour_limit_s`, `pickup_eta_s`, `t_pd_s`, `t_db_s`, `dist_ap_m`, `dist_pd_m`, `dist_db_m`, `corridor_dist_m`, `departure_delta_s`, `snap_dist_pickup_m`, `snap_dist_dropoff_m`, `seats_available`, y las preferencias blandas del pasajero tal cual (`prefs`).

## R-11 · Validación del request

- **Decisión**: Pydantic v2: `requestId`/`passengerId` como `UUID`; `departureAt` `AwareDatetime` (se exige zona horaria; sin zona → 422, para no confundir hora local de Mérida con UTC); `timeWindowMin ≥ 0`; `limit ≥ 1` sin tope (contrato); bbox validado con `BBOX_*` → 422 con `loc` = `["body","pickup"]` o `["body","dropoff"]`, mismo formato que los errores nativos de FastAPI.
- **Pendiente menor**: confirmar con crud-ts que siempre envía offset (`-06:00` o `Z`). Si no, se acepta naive como `America/Merida` — se decide en implementación sin cambiar el contrato.

## R-12 · Estrategia de pruebas

- **Decisión**:
  - Unitarias (sin red): `respx` para mockear OSRM; fakes de `CandidateSource`, repo y Redis para el handler; `httpx.AsyncClient(transport=ASGITransport(app))` para probar FastAPI.
  - Integración (marcadas `@pytest.mark.integration`, opt-in): PostGIS real (`postgis/postgis:16-3.4`) cargando `tests/fixtures/crud_ts_schema.sql` para validar la query (dirección, radio, ventana, match activo).
  - Rendimiento: script de carga ligero (no en CI) contra OSRM real con el grafo de Yucatán y 80 rides sembrados, midiendo p95.
- **Nuevas dependencias**: runtime `fastapi`, `uvicorn[standard]`, `httpx`; dev `respx`. Se **descarta** `shapely` (R-05).
- **Hallazgo**: el `Dockerfile` instala dependencias **a mano** (lista fija), no desde `pyproject.toml`; hay que añadir ahí también las nuevas o cambiarlo a `uv pip install --system -r pyproject.toml`. Se elige lo segundo para tener una sola fuente de verdad.

## R-13 · Rendimiento (presupuesto p95 < 400 ms con 80 candidatos)

| Fase | Estimado p95 | Notas |
|------|--------------|-------|
| Validación + prefs | < 5 ms | |
| Query candidatos | 20–60 ms | requiere índices de R-06 |
| `/nearest` ×2 (paralelo) | 5–15 ms | |
| `/table` 82×82 | 30–80 ms | una sola llamada (R-03) |
| Scoring | < 2 ms | |
| `/route` ×3 (paralelo) | 15–40 ms | |
| Serialización + XADD (async) | < 5 ms | XADD fuera del camino crítico |
| **Total** | **~80–210 ms** | margen ~2× sobre 400 ms |

Snap y query de candidatos son independientes → se lanzan en paralelo (`asyncio.gather`) para recortar ~15 ms.

## R-14 · Riesgos de infraestructura detectados (fuera de alcance, se reportan)

1. `OSRM_URL=http://osrm:5000` en Fargate `awsvpc`: el nombre `osrm` sólo resuelve con ECS Service Connect / Cloud Map. Verificar antes de desplegar.
2. HealthCheck con `curl` en imagen slim (R-02).
3. Nombre de consumer fijo al escalar (R-08).
4. `Dockerfile` comenta "worker (no expone puertos)": hay que añadir `EXPOSE 8000` y ajustar el comentario.
