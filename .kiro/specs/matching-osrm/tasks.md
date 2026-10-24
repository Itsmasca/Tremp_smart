# Tasks — slice `matching` con OSRM

## T-01 · Nuevas dependencias y configuración

- [ ] Añadir a `pyproject.toml`: `fastapi>=0.115,<1`, `uvicorn[standard]>=0.30,<1`, `httpx>=0.27,<1`; dev: `respx` (sin `shapely`)
- [ ] Añadir a `app/shared/config.py` las variables: `OSRM_URL`, `OSRM_MAX_TABLE_SIZE`, `CANDIDATE_RADIUS_M`, `MAX_CANDIDATES`, `ROUTE_GEOMETRY_TOP_K`, `PICKUP_ETA_NORM_S`, `SCORE_W_DETOUR`, `SCORE_W_TIME`, `SCORE_W_ETA`, `HTTP_PORT`, `BBOX_*`
- [ ] Actualizar `.env.example` con las nuevas variables

## T-02 · Tipos OSRM (`app/shared/osrm/types.py`)

- [ ] Definir dataclasses `LatLng`, `SnappedPoint`, `Matrix`, `Route`
- [ ] `Matrix` con `durations: list[list[float | None]]` y `distances: list[list[float | None]]`

## T-03 · Cliente OSRM (`app/shared/osrm/client.py`)

- [ ] Crear `OsrmClient` con `httpx.AsyncClient` reutilizable (inicializado en `__init__`, cerrado con `aclose()`), timeouts `connect=1s` / `read=2s` configurables
- [ ] Implementar `nearest(point) -> SnappedPoint` — `GET /nearest/v1/driving/{lon},{lat}`
- [ ] Implementar `table(coords, sources, destinations) -> Matrix` — `GET /table/v1/driving/...`
  - Coordenadas en formato `lon,lat` (OSRM)
  - Manejar `code != "Ok"` → `OsrmError`
  - Preservar celdas `null` como `None`
  - `annotations=duration,distance`
  - Si `len(sources) × len(destinations) > OSRM_MAX_TABLE_SIZE²`, lanzar error (la partición la hace `detour.py`, ver T-07)
- [ ] Definir `OsrmUnavailable` (→ 503) y `OsrmNoSegment` (→ lista vacía) en `errors.py`
- [ ] Implementar `route(coords) -> Route` — `GET /route/v1/driving/...?geometries=geojson&overview=full`
- [ ] Un reintento automático ante `httpx.NetworkError` o timeout
- [ ] Definir `OsrmError` como excepción propia

## T-04 · Esquemas del slice (`app/features/matching/schema.py`)

- [ ] `LatLng`, `MatchRequest` con validadores de campos requeridos (`limit ≥ 1` sin tope superior, `timeWindowMin ≥ 0`)
- [ ] `Candidate`, `MatchResponse`
- [ ] `MatchShownCandidate`, `MatchShownData`, `MatchShownEnvelope`
- [ ] `RideRow` (dataclass interno con todos los campos de la tabla `rides`)

## T-05 · Repository solo lectura (`app/features/matching/repository.py`)

- [ ] `get_passenger_preferences(conn, passenger_id) -> StructuredPreferences | None`
- [ ] Rides y matches activos NO se consultan aquí: ya los resuelve la query única de `PostGISCandidateSource` (T-06)
- [ ] Ninguna función escribe ni recalcula `route` / `duration_s` / `distance_m` (propiedad de crud-ts, C-07)

## T-06 · Protocolo e implementación `CandidateSource` (`app/features/matching/candidates.py`)

- [ ] Definir `Protocol` `CandidateSource` con método `fetch(...) -> list[RideRow]`
- [ ] Implementar `PostGISCandidateSource` con la query SQL de §5.2 del design
  - Filtros: status, seats, departure_at window, driver ≠ passenger, sin match activo, `ST_DWithin` × 2, `ST_LineLocatePoint` dirección
  - Tope `MAX_CANDIDATES` ordenado por `corridor_dist`
- [ ] Parámetros leídos de `settings`

## T-07 · Cálculo de desvío (`app/features/matching/detour.py`)

- [ ] Función `compute_detours(rides, snapped_p, snapped_d, osrm, passenger_prefs) -> list[ViableCandidate]`
- [ ] Construir `coords = [A₁…Aₙ, P, D, B₁…Bₙ]` con índices correctos para sources/destinations
- [ ] Una sola llamada a `osrm.table(...)`; solo si `n > max_batch_candidates(OSRM_MAX_TABLE_SIZE)` (= `N − 2`), partir en lotes **incluyendo P y D en cada lote**, lanzados en paralelo (design §4.3)
- [ ] Extraer `t_ap_i`, `t_pd`, `t_db_i` de la matriz; calcular `detour_i` y `eta_i`
- [ ] Descarte si celda `null` o `detour_i > ride.max_detour_s`
- [ ] Filtro duro de preferencias: `detour_i > passenger_prefs.max_detour_minutes * 60`
- [ ] Desvío negativo → `max(detour_i, 0)` para score, valor real en log

## T-08 · Scoring (`app/features/matching/scoring.py`)

- [ ] Definir `Protocol` `RankingStrategy` con `model_version: str` y `score(candidates) -> list[ScoredCandidate]`
- [ ] Implementar `HeuristicV1` con la fórmula de §7.2 del design (usar `timedelta.total_seconds()`, no `.seconds`)
- [ ] Pesos configurables desde `settings`
- [ ] Ordenamiento: `score DESC`, `detourS ASC`, `rideId ASC`

## T-09 · ~~KNN por embeddings~~ — eliminada

Fuera de alcance de este slice (ver requirements §4). Si el ranker la necesita, va en su propia spec.

## T-10 · Router HTTP (`app/features/matching/router.py`) + `GET /health` (`app/shared/http/app.py`)

- [ ] `GET /health` → 200 `{"status":"ok"}` sin tocar DB ni OSRM

- [ ] Crear `APIRouter` con `POST /match`
- [ ] Validar bbox Yucatán → 422 con mensaje que identifica el campo
- [ ] Orquestar: candidatos → snap (paralelo con `asyncio.gather`) → desvíos → scoring → trazado top-K → publicar `match.shown` (fire-and-forget con `asyncio.create_task`)
- [ ] Log estructurado con `requestId`, conteos y tiempos de cada fase
- [ ] Capturar `OsrmError` sin ruta posible → descartar candidato (no fallar toda búsqueda)
- [ ] Capturar `OsrmError` de indisponibilidad total → 503

## T-11 · Trazado top-K (`router.py` o utilidad en `detour.py`)

- [ ] Solo para los primeros `ROUTE_GEOMETRY_TOP_K` candidatos rankeados
- [ ] Llamar `osrm.route([Aᵢ, P, D, Bᵢ])` en paralelo con `asyncio.gather`
- [ ] Usar `routes[0].geometry` de OSRM (`geometries=geojson`) tal cual; si una llamada falla → `routeGeometry = null` + log (no 503)
- [ ] El resto de candidatos llevan `routeGeometry = null`

## T-12 · Publicación de `match.shown` (`app/features/matching/events.py`)

- [ ] Construir `MatchShownEnvelope` siguiendo el mismo formato que `PreferencesUpdatedEnvelope` (`aggregateType="match_request"`, `data` con `timeWindowMin`, `pickup`/`dropoff` ajustados)
- [ ] Guardar las tasks en un `set` con `add_done_callback` para que no las recoja el GC y loguear su excepción
- [ ] `XADD rides.events` con campo `envelope` (idéntico al formato del slice preferences)
- [ ] Fire-and-forget: `asyncio.create_task(...)` — nunca bloquea ni propaga excepción

## T-13 · Integrar HTTP + consumer en `main.py`

- [ ] Añadir imports de FastAPI, uvicorn, `OsrmClient`, `matching_router`
- [ ] Crear la app FastAPI e inyectar dependencias (db_pool, redis, osrm) vía `app.state` o `Depends`
- [ ] `asyncio.gather(server.serve(), run_consumer(...))` para correr ambos en el mismo event loop
- [ ] Añadir `HTTP_PORT` a `Settings` y actualizar `Dockerfile` con `EXPOSE`

## T-14 · Fixture de esquema para tests (`tests/fixtures/crud_ts_schema.sql`)

- [ ] **No** crear `migrations/002_*`: este slice no tiene DDL sobre tablas de crud-ts (C-08)
- [ ] Réplica mínima de `rides`, `matches`, `user_preferences` + `CREATE EXTENSION postgis`, solo para tests (según §13 del design)
- [ ] Índices GIST en `route`, índices en `departure_at` y `status` (para que la query de tests se parezca a producción)
- [ ] Cargarla desde `tests/conftest.py` en los tests de integración, nunca desde `main.py` ni el Dockerfile

## T-15 · Tests del cliente OSRM (`tests/unit/shared/test_osrm_client.py`)

- [ ] Usar `respx` para mockear respuestas HTTP
- [ ] `test_nearest_ok` — respuesta válida → `SnappedPoint` correcto
- [ ] `test_table_ok` — matrix con sources/destinations → índices correctos
- [ ] `test_table_null_cells` — celdas null preservadas como `None`
- [ ] `test_table_too_big` — `sources × destinations > OSRM_MAX_TABLE_SIZE²` → error explícito (la partición se prueba en T-17)
- [ ] `test_route_ok` — geometría GeoJSON devuelta
- [ ] `test_retry_on_network_error` — primer intento falla, segundo OK
- [ ] `test_osrm_unavailable` — dos fallos → `OsrmError` propagada

## T-16 · Tests de candidatos (`tests/unit/matching/test_candidates.py`)

- [ ] Mock de asyncpg; verificar que la query incluye todos los filtros
- [ ] Ride en radio correcto y sentido correcto → aparece
- [ ] Ride en sentido inverso → no aparece
- [ ] Ride con match activo → no aparece
- [ ] Ride sin asientos → no aparece
- [ ] Ride fuera de ventana horaria → no aparece

## T-17 · Tests de desvío (`tests/unit/matching/test_detour.py`)

- [ ] Construcción correcta de índices de la matriz para n candidatos
- [ ] Celda null → candidato descartado
- [ ] `detour_i > max_detour_s` → descartado
- [ ] `detour_i < 0` → `max(0, detour_i)` en score, valor real en log
- [ ] Filtro preferencias `max_detour_minutes` más restrictivo que el del conductor
- [ ] Con 80 candidatos y `OSRM_MAX_TABLE_SIZE=100` → **una sola** llamada `/table`
- [ ] Partición en lotes: con `OSRM_MAX_TABLE_SIZE=10` y 20 candidatos → varios lotes, todos con P y D; resultado idéntico a una matriz única

## T-18 · Tests de scoring (`tests/unit/matching/test_scoring.py`)

- [ ] Score correcto con pesos default
- [ ] Pesos configurables producen score diferente
- [ ] Ordenamiento: score DESC, desempate detourS ASC, rideId ASC
- [ ] Todos los scores en [0, 1]

## T-19 · ~~Tests de KNN~~ — eliminada (ver T-09)

## T-20 · Tests del router (`tests/unit/matching/test_router.py`)

- [ ] Happy path: request válido → 200 con candidatos rankeados
- [ ] Pickup fuera del bbox → 422
- [ ] Dropoff fuera del bbox → 422
- [ ] OSRM no disponible → 503
- [ ] Sin candidatos viables → 200 con `candidates: []`
- [ ] `match.shown` publicado en Redis (mock `xadd`)
- [ ] Fallo de publicación `match.shown` no afecta la respuesta

## T-21 · ~~Script de simulación~~ — eliminada

Mezclaba el slice preferences (publicar `preferences.updated`) y el KNN; fuera de alcance. El happy path de `POST /match` ya queda cubierto por T-20.

## T-22 · Actualizar `Dockerfile` y `.env.example`

- [ ] Añadir `EXPOSE 8000` en el Dockerfile y cambiar la instalación a `uv pip install --system .` (desde `pyproject.toml`)
- [ ] Cambiar el healthCheck de `infra/task-def-v2.json`: `curl` no existe en `python:3.13-slim` → usar `python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)"`
- [ ] Añadir todas las nuevas variables al `.env.example` con valores por defecto comentados
- [ ] Actualizar el README con la sección del slice matching

---

## Orden de implementación sugerido

```
T-01 → T-02 → T-03 → T-04
                          ↓
T-05 → T-06 → T-07 → T-08
                          ↓
       T-10 → T-11 → T-12 → T-13
                                  ↓
T-14 → T-15 → T-16 → T-17 → T-18 → T-20 → T-22
```

Los bloques T-02/T-03 (OSRM) y T-05/T-06/T-07 (candidatos + desvío) son independientes entre sí y se pueden trabajar en paralelo después de T-01 y T-04.
