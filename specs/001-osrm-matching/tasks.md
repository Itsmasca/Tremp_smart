---

description: "Tareas de implementación del slice matching con OSRM"
---

# Tasks: Slice `matching` con desvío exacto (OSRM)

**Input**: Documentos de diseño en `specs/001-osrm-matching/`

**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md), [data-model.md](./data-model.md), [contracts/](./contracts/), [quickstart.md](./quickstart.md)

**Tests**: Incluidos. La spec define criterios verificables (SC-002/003/005/006) y el repo sigue la convención de probar sin servicios externos reales (gate G6 del plan). Dentro de cada historia se escriben primero las pruebas y deben fallar antes de implementar.

**Organization**: Por historia de usuario, en orden de prioridad: US1 (P1) → US2 (P2) → US4 (P2) → US3 (P3).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: paralelizable (archivo distinto, sin dependencias pendientes)
- **[Story]**: US1–US4 según spec.md
- Rutas relativas a la raíz del repo (proyecto único, vertical slices: `app/features/…`, `app/shared/…`)

**Reglas transversales para TODAS las tareas** (no repetir en cada una):
- Las tablas `rides`, `matches` y `user_preferences` son solo lectura. No hay DDL sobre ellas fuera de `tests/fixtures/`, y nunca se crea ni recalcula `rides.route` (FR-023a/b).
- No se importa nada de `app/shared/llm/` ni `boto3` desde `app/features/matching/` (FR-024).
- El orden lon,lat solo aparece dentro de `app/shared/osrm/client.py`; el resto del código usa `LatLng(lat, lng)`.
- Los logs siguen el formato JSON de `main.py:_configure_logging`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: dependencias, configuración y esqueleto de paquetes.

- [X] T001 Añadir a `pyproject.toml` las dependencias `fastapi>=0.115,<1`, `uvicorn[standard]>=0.30,<1`, `httpx>=0.27,<1` y, en el extra `dev`, `respx>=0.21,<1`. Registrar el marcador `integration` en `[tool.pytest.ini_options]` (`markers = ["integration: requiere PostGIS real"]`) y excluirlo por defecto (`addopts = "-m 'not integration'"`). No añadir `shapely` (research R-05).
- [X] T002 [P] Crear los paquetes vacíos con `__init__.py`: `app/features/matching/`, `app/shared/osrm/`, `app/shared/http/`, `tests/unit/matching/`, `tests/integration/`, `tests/integration/matching/`, `tests/fixtures/`.
- [X] T003 Extender `Settings` en `app/shared/config.py` con las variables de data-model.md §5 (`HTTP_PORT`, `OSRM_URL`, `OSRM_MAX_TABLE_SIZE`, `OSRM_CONNECT_TIMEOUT_S`, `OSRM_READ_TIMEOUT_S`, `CANDIDATE_RADIUS_M`, `MAX_CANDIDATES`, `ROUTE_GEOMETRY_TOP_K`, `PICKUP_ETA_NORM_S`, `SCORE_W_DETOUR`, `SCORE_W_TIME`, `SCORE_W_ETA`, `BBOX_LAT_MIN/MAX`, `BBOX_LNG_MIN/MAX`, `MATCH_TERMINAL_STATUSES` como lista separada por comas, `DB_POOL_MAX_SIZE`) con sus defaults. Añadir un `model_validator` que exija: `OSRM_MAX_TABLE_SIZE ≥ 3`, pesos `≥ 0` con suma `> 0`, `BBOX_*_MIN < *_MAX`. Mantener el estilo de secciones con comentarios `# ── … ──` del archivo.
- [X] T004 [P] Añadir las nuevas variables, con su default y un comentario, a `.env.example`, siguiendo el formato de secciones existente. Indicar en el comentario de `OSRM_MAX_TABLE_SIZE` que debe coincidir con `--max-table-size` del contenedor OSRM.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: cliente OSRM, esquemas del contrato, app HTTP y arranque conjunto con el consumer. Todas las historias dependen de esta fase.

**⚠️ CRITICAL**: ninguna historia empieza hasta terminar esta fase.

- [X] T005 [P] Crear `app/shared/osrm/types.py` con dataclasses `frozen`: `LatLng(lat: float, lng: float)`, `SnappedPoint(location: LatLng, distance_m: float)`, `Matrix(durations: list[list[float | None]], distances: list[list[float | None]])` y `Route(duration_s: float, distance_m: float, geometry: dict)`.
- [X] T006 [P] Crear `app/shared/osrm/errors.py` con `OsrmError(Exception)` (incluye `code: str | None` y `message`), `OsrmUnavailable(OsrmError)` (transporte, timeout o 5xx tras el reintento → se convierte en 503), `OsrmNoSegment(OsrmError)` (`code == "NoSegment"`) y `OsrmTooBig(OsrmError)` (`code == "TooBig"` o validación local).
- [X] T007 [P] Escribir `tests/unit/shared/test_osrm_client.py` con `respx` (deben fallar hasta T008). Casos:
  - `test_nearest_ok`: arma la URL `/nearest/v1/driving/{lng},{lat}?number=1` en orden lon,lat y devuelve `SnappedPoint` desde `waypoints[0].location`/`distance`.
  - `test_nearest_no_segment` → `OsrmNoSegment`.
  - `test_table_ok`: query `sources=0;1&destinations=1;2&annotations=duration,distance` y matriz mapeada.
  - `test_table_null_cells`: las celdas `null` se conservan como `None`.
  - `test_table_too_big_local`: `len(sources) × len(destinations) > OSRM_MAX_TABLE_SIZE²` → `OsrmTooBig` sin hacer la petición HTTP.
  - `test_route_ok`: `overview=full&geometries=geojson&steps=false` y la geometría se devuelve tal cual.
  - `test_retry_on_transport_error`: el primer intento lanza `httpx.ConnectError` y el segundo responde OK.
  - `test_unavailable_after_retry`: dos timeouts → `OsrmUnavailable`.
  - `test_5xx_unavailable`.
  - `test_no_retry_on_code_error`: `code="InvalidQuery"` → `OsrmError` con una sola llamada.
- [X] T008 Implementar `OsrmClient` en `app/shared/osrm/client.py` (depende de T005 y T006):
  - Constructor `__init__(base_url, max_table_size, connect_timeout_s, read_timeout_s, client: httpx.AsyncClient | None = None)` con `httpx.AsyncClient(base_url=…, timeout=httpx.Timeout(read_timeout_s, connect=connect_timeout_s), limits=httpx.Limits(max_connections=50, max_keepalive_connections=20))`.
  - Métodos `async nearest(point)`, `async table(coords, sources, destinations)`, `async route(coords)` y `async aclose()`.
  - Un helper privado `_get(path, params)` que hace un reintento ante `httpx.TransportError`/`TimeoutException`, convierte 5xx en `OsrmUnavailable` y `code != "Ok"` en la excepción que corresponda.
  - Formatear coordenadas solo aquí con `f"{p.lng:.6f},{p.lat:.6f}"` unidas por `;`.
  - Exportar `OsrmClient` y los tipos desde `app/shared/osrm/__init__.py`.
  - Debe pasar T007.
- [X] T009 [P] Crear `app/features/matching/schema.py` con Pydantic v2 según [contracts/match-api.openapi.yaml](./contracts/match-api.openapi.yaml):
  - `LatLngIn(lat: float, lng: float)` con rangos −90..90 y −180..180.
  - `MatchRequest`: `requestId: UUID`, `passengerId: UUID`, `pickup`, `dropoff`, `departureAt: AwareDatetime`, `timeWindowMin: int = Field(30, ge=0)`, `limit: int = Field(10, ge=1)`.
  - `CandidateOut`, con los campos en camelCase exactamente como en el contrato y `routeGeometry: dict | None`.
  - `MatchResponse`.
  - Dataclasses internas de data-model.md §3: `RideRow`, `DetourResult` y `ScoredCandidate`.
- [X] T010 Crear `app/shared/http/app.py` con `create_app(*, db_pool, redis, osrm, candidate_source, ranker) -> FastAPI`. Guarda las dependencias en `app.state`, expone `GET /health` → `{"status": "ok"}` sin tocar la DB ni OSRM, incluye un `register_routers(app)` para que cada slice monte su router y registra un `exception_handler` para `OsrmUnavailable` → `JSONResponse(503, {"detail": "routing engine unavailable"})`. Depende de T006.
- [X] T011 Modificar `main.py` (depende de T003, T008 y T010):
  - Crear el pool con `max_size=settings.DB_POOL_MAX_SIZE`, `OsrmClient` desde `settings` y la app con `create_app(...)`.
  - Configurar `uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=settings.HTTP_PORT, log_config=None))`.
  - Correr `server.serve()` y el `run_consumer(...)` existente con `asyncio.gather` en el mismo loop. Si cualquiera de los dos termina o lanza, cancelar el otro, cerrar `osrm.aclose()`, el pool y redis, y salir con código ≠ 0.
  - Mantener intacto el registro de `"preferences.updated"`.
- [X] T012 [P] Escribir `tests/unit/shared/test_http_app.py`: `GET /health` → 200 `{"status":"ok"}` usando `httpx.AsyncClient(transport=httpx.ASGITransport(app))` con dependencias falsas; ninguna dependencia recibe llamadas.

**Checkpoint**: `uv run python main.py` levanta HTTP en :8000 y el consumer; `curl localhost:8000/health` → ok; la suite existente de preferences sigue en verde.

---

## Phase 3: User Story 1 - Pasajero encuentra rides viables (Priority: P1) 🎯 MVP

**Goal**: `POST /match` devuelve los rides viables (filtros de estado, asientos, ventana, conductor, match activo, corredor, dirección), con desvío exacto, ETA, puntos ajustados, ordenados con `heuristic-v1` y recortados a `limit`. En esta fase `routeGeometry` siempre es `null`.

**Independent Test**: escenarios R1–R7 de [quickstart.md §3](./quickstart.md#3-escenarios-de-validación). Solo R1 aparece; `detourS` coincide con `/route` en < 1 s (SC-003); sin rides viables → 200 `[]`.

### Tests for User Story 1 ⚠️ (escribir primero, deben fallar)

- [X] T013 [P] [US1] Crear `tests/fixtures/crud_ts_schema.sql` con la réplica de pruebas de design Kiro §13: `CREATE EXTENSION IF NOT EXISTS postgis`, tablas `rides`, `matches` y `user_preferences`, índice GIST en `rides.route` e índice en `departure_at`. Encabezado con el comentario `-- SOLO TESTS: el esquema real es de crud-ts. No es una migración.`
- [X] T014 [P] [US1] Escribir `tests/integration/matching/test_candidates_postgis.py` (`@pytest.mark.integration`, DSN de `TEST_DATABASE_URL`, salta si no existe). Carga T013 en un schema temporal, siembra R1–R7 de quickstart §3 con LineStrings simples en Mérida y verifica que `PostGISCandidateSource.fetch` devuelve solo R1. Añadir casos de radio (ruta a 1.6 km → excluida), de tope `max_candidates` y de orden por `corridor_dist_m`.
- [X] T015 [P] [US1] Escribir `tests/unit/matching/test_detour.py` con un `OsrmClient` falso. Casos:
  - Para n=3, `coords = [A1,A2,A3,P,D,B1,B2,B3]`, `sources=[0..4]` y `destinations=[3,4,5,6,7]`.
  - `t_ap = m[i][0]`, `t_pd = m[n][1]`, `t_db = m[n+1][2+i]`.
  - `detour_raw = t_ap + t_pd + t_db − duration_s`.
  - Celda `None` → descarte `no_route`.
  - `t_pd` nulo → todos `no_route`.
  - `max(detour,0) > max_detour_s` → `detour_driver`.
  - `prefs.max_detour_minutes` más restrictivo → `detour_passenger`, con `detour_limit_s = min(...)`.
  - Desvío negativo → se conserva `detour_s_raw < 0` y el filtro usa 0.
  - 80 candidatos con `OSRM_MAX_TABLE_SIZE=100` → **una** llamada a `table`.
  - `OSRM_MAX_TABLE_SIZE=10` con 20 candidatos → varios lotes con P y D en cada uno y resultado idéntico al cálculo sin lotes.
- [X] T016 [P] [US1] Escribir `tests/unit/matching/test_scoring.py`:
  - Fórmula con los pesos default sobre valores conocidos.
  - Pesos distintos → score distinto.
  - `timeWindowMin=0` → `time_n=0`.
  - `detour_limit_s=0` con desvío ≤ 0 → `detour_n=0`.
  - Score siempre en [0,1], también con ETA > norma.
  - Desempate por score DESC, `max(detour,0)` ASC y `rideId` ASC.
  - `rank` consecutivo desde 1.
  - `model_version == "heuristic-v1"`.
- [X] T017 [P] [US1] Escribir `tests/unit/matching/test_router.py` (parte US1) con `ASGITransport`, un `CandidateSource` falso, un `OsrmClient` falso y un pool falso para preferencias. Casos:
  - Happy path con 3 viables y `limit=2` → 2 candidatos con `rank` 1–2, score descendente, `snappedPickup`/`snappedDropoff` iguales en ambos y `routeGeometry` nulo.
  - Sin rides → 200 `{"candidates": []}` sin llamar a `table`.
  - Todos descartados → 200 `[]`.
  - `modelVersion == "heuristic-v1"`.
  - `requestId` como eco.

### Implementation for User Story 1

- [X] T018 [US1] Implementar `app/features/matching/candidates.py` (depende de T009):
  - `class CandidateSource(Protocol)` con `async fetch(*, pickup: LatLng, dropoff: LatLng, departure_at, time_window_min, passenger_id, max_candidates) -> list[RideRow]`.
  - `PostGISCandidateSource(pool, radius_m, terminal_statuses)` con una sola query parametrizada (asyncpg `$n`):
    - CTE `pts` con `p`/`d` construidos con `ST_SetSRID(ST_MakePoint(lng, lat), 4326)`.
    - Prefiltro en geometry con `ST_DWithin(r.route, p, radius_deg)`, donde `radius_deg = radius_m / (111320 * cos(radians(lat)))` se calcula en Python con margen de 1.1×.
    - Filtro exacto en geography `ST_DWithin(r.route::geography, p::geography, radius_m)` para p y d.
    - Dirección con `ST_LineLocatePoint(r.route, p) < ST_LineLocatePoint(r.route, d)`.
    - `status='scheduled'`, `seats_available > 0`, `driver_id <> passenger_id`, `departure_at BETWEEN dep − w AND dep + w`.
    - `NOT EXISTS` sobre `matches` con `status <> ALL($terminal_statuses)`.
    - `ORDER BY corridor_dist_m LIMIT $max`.
    - Devolver `origin`/`destination` como `ST_Y`/`ST_X` → `LatLng`.
  - No seleccionar `route` (no se necesita en Python).
  - Debe pasar T014.
- [X] T019 [P] [US1] Implementar `app/features/matching/preferences.py`: `async get_passenger_preferences(conn, passenger_id) -> StructuredPreferences | None` con `SELECT structured FROM user_preferences WHERE user_id = $1`. Importar solo `app.features.preferences.schema.StructuredPreferences`. Si la fila es nula o no existe → `None`. Si falla `model_validate` → `None` + `logger.warning("prefs_invalid | passengerId=%s", …)`. Acepta `structured` como `str` o `dict` (asyncpg devuelve jsonb como str si no hay codec).
- [X] T020 [US1] Implementar `app/features/matching/detour.py` (depende de T008 y T009):
  - `max_batch_candidates(max_table_size) -> int` (= `max_table_size − 2`, research R-03).
  - `async compute_detours(rides, snapped_p, snapped_d, osrm, prefs) -> tuple[list[DetourResult], Counter]` que construye la matriz de [data-model.md §3](./data-model.md#3-modelos-internos-por-request-en-memoria), parte en lotes en paralelo solo si `len(rides) > max_batch_candidates` (P y D en cada lote; `t_pd` del primero), aplica las reglas de descarte en orden y cuenta los motivos (`no_route`, `detour_driver`, `detour_passenger`). Rellena `d_ap_m`/`d_pd_m`/`d_db_m` desde `distances`.
  - Debe pasar T015.
- [X] T021 [P] [US1] Implementar `app/features/matching/scoring.py`:
  - `class RankingStrategy(Protocol)` con `model_version: str` y `rank(results, *, departure_at, time_window_min) -> list[ScoredCandidate]`.
  - `HeuristicV1(w_detour, w_time, w_eta, eta_norm_s)` que normaliza los pesos si no suman 1 (con log de aviso), calcula score y features (research R-10), ordena y asigna `rank`.
  - Función pura, sin I/O. Debe pasar T016.
- [X] T022 [US1] Implementar `app/features/matching/router.py` (depende de T018–T021 y T010):
  - `router = APIRouter()` con `POST /match` y `response_model=MatchResponse`.
  - En paralelo con `asyncio.gather`: `candidate_source.fetch(...)`, `get_passenger_preferences` (con su propia conexión del pool) y `osrm.nearest(pickup)`/`osrm.nearest(dropoff)`.
  - `OsrmNoSegment` → 200 `[]`.
  - Si no hay rides → 200 `[]`.
  - Si hay: `compute_detours` → `ranker.rank(...)` → `[:limit]` → `CandidateOut` con `detourS=round(detour_s_raw)`, `pickupEtaS=round(t_ap_s)` y `routeGeometry=None`.
  - Medir el tiempo por fase con `time.perf_counter()` y emitir un `logger.info` con `requestId`, `generated`, los motivos de descarte, `returned` y `ms_*` por fase.
  - Registrar el router en `create_app` (T010).
  - Debe pasar T017.

**Checkpoint**: MVP funcional; quickstart §3.1 y §3.4 pasan contra la infra local.

---

## Phase 4: User Story 2 - Recorrido en el mapa para las mejores opciones (Priority: P2)

**Goal**: los primeros `ROUTE_GEOMETRY_TOP_K` candidatos traen `routeGeometry` (GeoJSON LineString A→P→D→B); el resto, `null`.

**Independent Test**: con ≥ 4 viables y K=3, ranks 1–3 con LineString y rank 4+ en `null`; si una llamada `/route` falla, ese candidato queda en `null` y la respuesta sigue siendo 200.

### Tests for User Story 2 ⚠️

- [X] T023 [P] [US2] Añadir a `tests/unit/matching/test_router.py`:
  - 5 viables con K=3 → geometría solo en ranks 1–3, cada una con `type == "LineString"`.
  - `osrm.route` se llama con `[A_i, P', D', B_i]`.
  - Fallo de `route` en rank 2 → `null` en ese candidato, resto intacto, 200.
  - K=0 → ninguna llamada a `route`.

### Implementation for User Story 2

- [X] T024 [US2] En `app/features/matching/router.py`, añadir `_attach_geometry(ranked, snapped_p, snapped_d, osrm, top_k)`. Lanza en paralelo `osrm.route([ride.origin, P', D', ride.destination])` para los primeros `top_k` con `asyncio.gather(..., return_exceptions=True)`, asigna `route.geometry` y, ante excepción, deja `None` + `logger.warning`. Integrarlo tras el recorte a `limit` y añadir `ms_route` al log. Debe pasar T023.

**Checkpoint**: US1 + US2 funcionan; quickstart §3.1 muestra la geometría del top 1.

---

## Phase 5: User Story 4 - Errores claros (Priority: P2)

**Goal**: 422 para requests inválidos o fuera del bbox de Yucatán; 503 si OSRM no responde tras el reintento (nunca `[]` engañosa).

**Independent Test**: quickstart §3.3 (Monterrey → 422 `loc=["body","pickup"]`; `departureAt` inválido → 422; OSRM detenido → 503).

### Tests for User Story 4 ⚠️

- [X] T025 [P] [US4] Añadir a `tests/unit/matching/test_router.py`:
  - pickup fuera del bbox → 422 con `detail[0].loc == ["body","pickup"]`.
  - dropoff fuera → `["body","dropoff"]`.
  - `departureAt` sin zona → 422.
  - `limit=0` → 422.
  - `timeWindowMin=-1` → 422.
  - `nearest` lanza `OsrmUnavailable` → 503 `{"detail":"routing engine unavailable"}`.
  - `table` lanza `OsrmUnavailable` → 503.
  - En los casos 503 no se publica `match.shown`.

### Implementation for User Story 4

- [X] T026 [US4] En `app/features/matching/router.py`, validar el bbox con `settings.BBOX_*` antes de cualquier I/O. Si falla, lanzar `fastapi.exceptions.RequestValidationError([{"loc": ("body", campo), "msg": "coordenada fuera del área de servicio (península de Yucatán)", "type": "value_error.out_of_bbox", "input": …}])` para que la forma sea idéntica a la de los 422 nativos. Confirmar que `OsrmUnavailable` sube sin capturarse hasta el handler de T010. Debe pasar T025.

**Checkpoint**: US1 + US2 + US4 funcionan.

---

## Phase 6: User Story 3 - Registro de lo mostrado (`match.shown`) (Priority: P3)

**Goal**: cada búsqueda con ≥ 1 candidato publica un `match.shown` en `rides.events` sin añadir latencia ni hacer fallar la respuesta.

**Independent Test**: quickstart §3.2. `XREVRANGE` muestra el envelope válido contra [match-shown.event.schema.json](./contracts/match-shown.event.schema.json); con Redis caído la respuesta sigue siendo 200.

### Tests for User Story 3 ⚠️

- [X] T027 [P] [US3] Escribir `tests/unit/matching/test_events.py`:
  - `build_match_shown(...)` produce un dict válido contra `specs/001-osrm-matching/contracts/match-shown.event.schema.json`. Validar con `jsonschema` (añadirlo al extra `dev` en `pyproject.toml`).
  - `aggregateId == requestId`.
  - `publish_match_shown` llama `redis.xadd(stream, {"envelope": json})`.
  - Si `xadd` lanza, la excepción no se propaga y se registra en el log.
- [X] T028 [P] [US3] Añadir a `tests/unit/matching/test_router.py`:
  - Respuesta con candidatos → exactamente una llamada `xadd` tras la respuesta (esperar las tasks pendientes del publisher en el test).
  - Respuesta `[]` → ninguna llamada.
  - `xadd` que lanza → la respuesta sigue siendo 200.

### Implementation for User Story 3

- [X] T029 [US3] Implementar `app/features/matching/events.py`:
  - `build_match_shown(req, response, scored, snapped_p, snapped_d) -> dict` con el envelope de [contracts/match-shown.event.schema.json](./contracts/match-shown.event.schema.json): `id=uuid4`, `type="match.shown"`, `version=1`, `source="matching-py"`, `aggregateType="match_request"`, `occurredAt` en UTC ISO-8601 y `features` por candidato.
  - `class MatchShownPublisher(redis, stream)` con `publish(envelope)`, que hace `create_task` de `xadd(stream, {"envelope": json.dumps(envelope)})`, guarda la task en un `set` y le añade `add_done_callback` para descartarla y loguear su excepción.
  - Método `async drain()` para tests y apagado.
  - Debe pasar T027.
- [X] T030 [US3] Conectar `MatchShownPublisher` (en `create_app`/`app.state`, construido en `main.py` con `settings.REDIS_STREAM`) en `app/features/matching/router.py`: publicar solo si hay ≥ 1 candidato y después de construir la respuesta. En el apagado de `main.py`, llamar a `drain()` con un timeout corto. Debe pasar T028.

**Checkpoint**: las 4 historias funcionan de forma independiente.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [X] T031 [P] Actualizar `Dockerfile`: instalar dependencias desde `pyproject.toml` (`COPY pyproject.toml README.md ./` + `uv pip install --system --no-cache .`, o `uv pip compile` → requirements) en lugar de la lista fija actual; añadir `EXPOSE 8000`; cambiar el comentario "worker (no expone puertos)".
- [X] T032 [P] Cambiar el `healthCheck.command` de `infra/task-def-v2.json` a `["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)\" || exit 1"]` (`curl` no existe en `python:3.13-slim`, research R-02).
- [X] T033 [P] Crear `scripts/check_osrm_table_limit.py` (ejecutable con `python -m scripts.check_osrm_table_limit --n 80`). Arma una petición `/table` con `n` orígenes y destinos aleatorios dentro de Mérida usando `OsrmClient` (con la validación local desactivada para la prueba) y muestra `code`. Sirve para confirmar la regla `sources × destinations ≤ N²` (research R-03, quickstart §5). Añadir `scripts/__init__.py`.
- [X] T034 [P] Actualizar `README.md`:
  - Sección "Slice matching" con el diagrama del flujo de `POST /match` y un enlace a `specs/001-osrm-matching/`.
  - Nuevas variables de entorno en la tabla existente.
  - Cómo correr `-m integration`.
  - Nota de que `tests/fixtures/crud_ts_schema.sql` no es una migración.
  - Riesgos de infra de research R-14.
- [X] T035 Ejecutar `uv run pytest` (unit) y `uv run pytest -m integration` con PostGIS local; corregir fallos.
- [ ] T036 Ejecutar la validación de [quickstart.md](./quickstart.md) §1–§6 contra OSRM real con el grafo de Yucatán: escenarios R1–R7, SC-003 (desvío vs `/route` < 1 s), 422/503, evento y p95 < 400 ms con 80 candidatos. Revisar con `EXPLAIN ANALYZE` que la query usa el índice GIST; si no, reportarlo a crud-ts (no crear índices aquí).

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Fase 1)**: sin dependencias.
- **Foundational (Fase 2)**: depende de Setup y bloquea todas las historias.
- **US1 (Fase 3)**: depende de Foundational. Es el MVP.
- **US2 (Fase 4)** y **US4 (Fase 5)**: dependen de US1, porque extienden `router.py`. Entre sí son independientes en lógica, pero tocan el mismo archivo, así que conviene hacerlas en secuencia.
- **US3 (Fase 6)**: depende de US1. Su lógica vive en un archivo propio (`events.py`); solo T030 toca `router.py`.
- **Polish (Fase 7)**: T031–T034 se pueden hacer desde que termina Foundational; T035–T036 al final.

### Grafo

```text
T001 → T002, T003 → T004
T003 → T005/T006 (P) → T007 → T008
T009 (P)    T006 → T010 → T011     T010 → T012
            │
            ▼
US1: T013/T014/T015/T016/T017 (P, tests) → T018, T019 (P), T021 (P) → T020 → T022
            │
            ├──► US2: T023 → T024
            ├──► US4: T025 → T026
            └──► US3: T027/T028 (P) → T029 → T030
                                         ▼
                              T031–T034 (P) → T035 → T036
```

### Within Each User Story

- Primero las pruebas, que deben fallar.
- Después schema/tipos, luego los servicios puros (`detour`, `scoring`) y los adaptadores (`candidates`, `preferences`), y al final el router.

---

## Parallel Example: User Story 1

```text
# Pruebas en paralelo (archivos distintos):
T013 tests/fixtures/crud_ts_schema.sql
T014 tests/integration/matching/test_candidates_postgis.py
T015 tests/unit/matching/test_detour.py
T016 tests/unit/matching/test_scoring.py
T017 tests/unit/matching/test_router.py

# Implementación en paralelo:
T018 app/features/matching/candidates.py
T019 app/features/matching/preferences.py
T021 app/features/matching/scoring.py
# luego T020 (detour) → T022 (router)
```

## Parallel Example: Foundational

```text
T005 app/shared/osrm/types.py   T006 app/shared/osrm/errors.py   T009 app/features/matching/schema.py
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Fase 1 + Fase 2.
2. Fase 3 (US1).
3. **Validar**: quickstart §3.1 y §3.4, y escenarios R1–R7.
4. crud-ts ya puede integrarse: `routeGeometry` llega `null`, que el contrato permite.

### Incremental Delivery

1. MVP (US1).
2. US2: geometría en el mapa.
3. US4: 422 por bbox y 503 explícitos. Mientras no esté, FastAPI ya devuelve 422 por errores de tipo y una caída de OSRM daría 500.
4. US3: `match.shown` para el ranker.
5. Polish: Dockerfile, healthCheck, README y validación de rendimiento.

### Notas

- Un commit por tarea o grupo lógico; trabajar en la rama `001-osrm-matching` (hoy el repo está en `main`).
- T032 (healthCheck) **bloquea el despliegue** en ECS aunque no bloquea el desarrollo.
- Si T033 muestra que OSRM limita por número de coordenadas, cambiar solo `max_batch_candidates` (T020) a `(N − 2) // 2` y ajustar el caso de 80 candidatos de T015.
