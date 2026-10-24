# Design — slice `matching` con OSRM

## 1. Visión general

```
crud-ts  ──POST /match──►  router.py (FastAPI)
                                │
              ┌─────────────────┼──────────────────────┐
              ▼                 ▼                       ▼
        candidates.py      detour.py             scoring.py
        (PostGIS)          (OSRM table)          (heuristic-v1)
              │                 │                       │
              └─────────────────┴───────────────────────┘
                                │
                         repository.py        osrm/client.py
                         (solo lectura)       (httpx async)
                                │
                         rides.events (XADD match.shown)  ← fire-and-forget
```

El consumer del slice `preferences` sigue corriendo en el mismo proceso en su propio task asyncio.

---

## 2. Estructura de archivos nueva

```
app/
  features/
    matching/
      __init__.py
      router.py           # FastAPI APIRouter POST /match (orquestación, 422/503)
      schema.py           # MatchRequest, MatchResponse, Candidate, MatchShown*
      candidates.py       # PostGISCandidateSource + Protocol CandidateSource
      preferences.py      # solo lectura: user_preferences.structured del pasajero
      detour.py           # matriz OSRM [A…,P,D,B…], lotes, descarte con motivo
      scoring.py          # heuristic-v1 + Protocol RankingStrategy
      events.py           # build + publish match.shown (fire-and-forget)
  shared/
    http/
      __init__.py
      app.py              # create_app + GET /health + lifespan
    osrm/
      __init__.py
      client.py           # OsrmClient (httpx.AsyncClient, nearest/table/route)
      types.py            # LatLng, SnappedPoint, Matrix, Route
      errors.py           # OsrmError, OsrmUnavailable, OsrmNoSegment
tests/
  fixtures/
    crud_ts_schema.sql    # réplica mínima de rides/matches/user_preferences SOLO para tests
  unit/
    matching/
      test_candidates.py
      test_detour.py
      test_scoring.py
      test_router.py
    shared/
      test_osrm_client.py
```

---

## 3. Esquemas Pydantic (`schema.py`)

```python
class LatLng(BaseModel):
    lat: float
    lng: float

class MatchRequest(BaseModel):
    requestId:     str          # UUID, generado por crud-ts
    passengerId:   str          # UUID
    pickup:        LatLng
    dropoff:       LatLng
    departureAt:   datetime
    timeWindowMin: int = Field(default=30, ge=0)
    limit:         int = Field(default=10, ge=1)   # sin tope superior: no está en el contrato con crud-ts

class Candidate(BaseModel):
    rideId:         str
    rank:           int
    score:          float
    detourS:        int
    pickupEtaS:     int
    snappedPickup:  LatLng
    snappedDropoff: LatLng
    routeGeometry:  dict | None   # GeoJSON LineString o null

class MatchResponse(BaseModel):
    requestId:    str
    modelVersion: str
    candidates:   list[Candidate]

class MatchShownCandidate(BaseModel):
    rideId:      str
    rank:        int
    score:       float
    detourS:     int
    pickupEtaS:  int
    features:    dict             # preferencias + métricas para el ranker futuro

class MatchShownData(BaseModel):
    requestId:     str
    passengerId:   str
    modelVersion:  str
    departureAt:   str
    timeWindowMin: int
    pickup:        LatLng      # ajustado
    dropoff:       LatLng      # ajustado
    candidates:    list[MatchShownCandidate]

# Envelope: aggregateType = "match_request", aggregateId = requestId
# Esquema completo: specs/001-osrm-matching/contracts/match-shown.event.schema.json
```

---

## 4. Cliente OSRM (`shared/osrm/client.py`)

### 4.1 Tipos

```python
@dataclass
class LatLng:
    lat: float
    lng: float

@dataclass
class SnappedPoint:
    lat: float
    lng: float
    distance: float    # metros al punto original

@dataclass
class Matrix:
    durations:  list[list[float | None]]   # None = sin ruta
    distances:  list[list[float | None]]

@dataclass
class Route:
    duration: float    # segundos
    distance: float    # metros
    geometry: dict     # GeoJSON LineString
```

### 4.2 Métodos de `OsrmClient`

```python
class OsrmClient:
    async def nearest(self, point: LatLng) -> SnappedPoint
    async def table(self, coords: list[LatLng],
                    sources: list[int],
                    destinations: list[int]) -> Matrix
    async def route(self, coords: list[LatLng]) -> Route
```

- Coordenadas en URL: **lon,lat** (OSRM usa lon primero).
- `table` acepta índices `sources` y `destinations` relativos a `coords`.
- Si la respuesta tiene `code != "Ok"`, lanza `OsrmError`.
- Celdas `null` en la matriz se preservan como `None`.
- Reintento automático (1 reintento) ante `httpx.NetworkError` o timeout.

### 4.3 Partición en lotes

`OSRM_MAX_TABLE_SIZE` = `--max-table-size` del contenedor OSRM. OSRM rechaza (`TooBig`) un `/table` cuando `num_sources × num_destinations > N²` (ver speckit `research.md` R-03; **verificar con la imagen desplegada**, quickstart §5).

Con `sources=[A₁…Aₙ, P, D]` y `destinations=[P, D, B₁…Bₙ]` → `(n+2)² ≤ N²` ⇔ `n ≤ N − 2`. Con `N=100` y `MAX_CANDIDATES=80` **cabe en una sola llamada**.

La partición la hace `detour.py` (conoce la estructura `[A…, P, D, B…]`) y solo se activa si `n > max_batch_candidates(N)` (= `N − 2`): cada lote envía `[A_lote, P, D, B_lote]`, **P y D en todos los lotes**, en paralelo; resultado idéntico a una sola llamada. Si la verificación mostrara que el contenedor limita por número de coordenadas, solo cambia `max_batch_candidates` (→ `(N − 2) // 2`). `OsrmClient.table()` lanza error si recibe una petición que excede el límite, en vez de partir a ciegas.

Parámetros: `annotations=duration,distance`. Timeouts `connect=1s`, `read=2s`; 1 reintento ante error de transporte; sin reintento ante `code != "Ok"`.

---

## 5. Candidatos (`candidates.py`)

### 5.1 Protocolo

```python
class CandidateSource(Protocol):
    async def fetch(
        self,
        pickup: LatLng,
        dropoff: LatLng,
        departure_at: datetime,
        time_window_min: int,
        passenger_id: str,
        max_candidates: int,
    ) -> list[RideRow]: ...
```

`RideRow` contiene todos los campos que el handler necesita: `id`, `driver_id`, `origin`, `destination`, `route_geojson`, `duration_s`, `distance_m`, `departure_at`, `seats_available`, `max_detour_s`.

### 5.2 `PostGISCandidateSource`

Query SQL central:

```sql
SELECT r.id,
       r.driver_id,
       ST_AsGeoJSON(r.origin)::jsonb      AS origin,
       ST_AsGeoJSON(r.destination)::jsonb AS destination,
       ST_AsGeoJSON(r.route)::jsonb       AS route_geojson,
       r.duration_s,
       r.distance_m,
       r.departure_at,
       r.seats_available,
       r.max_detour_s,
       -- distancia al corredor para priorizar
       ST_Distance(r.route::geography,
                   ST_SetSRID(ST_MakePoint($lng_p, $lat_p), 4326)::geography)
       + ST_Distance(r.route::geography,
                   ST_SetSRID(ST_MakePoint($lng_d, $lat_d), 4326)::geography)
       AS corridor_dist
  FROM rides r
 WHERE r.status = 'scheduled'
   AND r.seats_available > 0
   AND r.driver_id <> $passenger_id
   AND r.departure_at BETWEEN $dep_min AND $dep_max
   AND NOT EXISTS (
       SELECT 1 FROM matches m
        WHERE m.ride_id = r.id
          AND m.passenger_id = $passenger_id
          AND m.status NOT IN ('rejected', 'completed', 'cancelled')
   )
   AND ST_DWithin(r.route::geography,
                  ST_SetSRID(ST_MakePoint($lng_p, $lat_p), 4326)::geography,
                  $radius_m)
   AND ST_DWithin(r.route::geography,
                  ST_SetSRID(ST_MakePoint($lng_d, $lat_d), 4326)::geography,
                  $radius_m)
   AND ST_LineLocatePoint(r.route,
                  ST_SetSRID(ST_MakePoint($lng_p, $lat_p), 4326))
     < ST_LineLocatePoint(r.route,
                  ST_SetSRID(ST_MakePoint($lng_d, $lat_d), 4326))
 ORDER BY corridor_dist
 LIMIT $max_candidates
```

---

## 6. Desvío (`detour.py`)

### 6.1 Snap

```python
snapped_p = await osrm.nearest(pickup)
snapped_d = await osrm.nearest(dropoff)
```

### 6.2 Construcción de la matriz

Para `n` candidatos se construye el array de coordenadas:
```
coords = [A₁, A₂, …, Aₙ, P, D, B₁, B₂, …, Bₙ]
```
Índices:
- `sources      = [0..n-1, n, n+1]`     → orígenes + P + D
- `destinations = [n, n+1, n+2..2n+1]` → P + D + destinos

Se extrae de la matriz:
```
t_ap_i  = matrix[i][0]       # Aᵢ → P
t_pd    = matrix[n][1]       # P  → D  (común a todos)
t_db_i  = matrix[n+1][2+i]  # D  → Bᵢ
detour_i = t_ap_i + t_pd + t_db_i - ride.duration_s
eta_i    = t_ap_i            # tiempo hasta el pickup
```

### 6.3 Desvío negativo
Si `detour_i < 0` (diferencias de grafo), se usa `max(detour_i, 0)` para filtro y score, conservando el valor real en el log.

---

## 7. Scoring (`scoring.py`)

### 7.1 Protocolo

```python
class RankingStrategy(Protocol):
    model_version: str
    def score(self, candidates: list[ScoredCandidate]) -> list[ScoredCandidate]: ...
```

### 7.2 `HeuristicV1`

```python
score_i = clip(w_d*(1 - detour_n) + w_t*(1 - time_n) + w_e*(1 - eta_n))

detour_n = clip(max(detour_s, 0) / detour_limit_s)
time_n   = clip(abs(ride_dep - req_dep).total_seconds() / (time_window_min*60))  # .seconds ignora días
# si time_window_min == 0 → time_n = 0 (solo pasan rides con la hora exacta)
eta_n    = clip(eta_s / PICKUP_ETA_NORM_S)
```

Pesos configurables: `SCORE_W_DETOUR=0.5`, `SCORE_W_TIME=0.3`, `SCORE_W_ETA=0.2`.

Ordenamiento: `score DESC`, `detourS ASC`, `rideId ASC`.

---

## 8. Propiedad de la ruta y de las tablas

- `rides.route`, `duration_s` y `distance_m` los genera `crud-ts` al publicar el ride. Este slice los **lee**; nunca los recalcula ni los escribe.
- El único cálculo de ruta propio es el recorrido con pasajero `Aᵢ → P → D → Bᵢ` (matriz para el desvío y `route` para el trazado top-K), que se calcula por request y **no se persiste**.
- Sin migraciones sobre `rides`, `matches` ni `user_preferences`. Los tests de integración usan `tests/fixtures/crud_ts_schema.sql` (ver §13).
- KNN por embeddings y simulación de eventos quedan fuera de este slice.

---

## 9. Router (`router.py`)

> Candidatos, preferencias y snap se lanzan en paralelo (`asyncio.gather`); el pseudocódigo de abajo los muestra en secuencia por claridad.

```python
router = APIRouter()

@router.post("/match", response_model=MatchResponse)
async def post_match(req: MatchRequest, ...):
    t0 = time.monotonic()

    # 1. Validar bbox Yucatán
    _validate_bbox(req.pickup, req.dropoff)

    # 2. Candidatos PostGIS
    rides = await candidates.fetch(req.pickup, req.dropoff, ...)

    # 3. Snap pickup + dropoff
    snapped_p, snapped_d = await asyncio.gather(
        osrm.nearest(req.pickup), osrm.nearest(req.dropoff)
    )

    # 4. Matriz OSRM → desvíos → filtrar
    viable = await detour.compute(rides, snapped_p, snapped_d, passenger_prefs)

    # 5. Scoring + ordenar + recortar a limit
    ranked = ranker.score(viable)[:req.limit]

    # 6. Trazado top-K (fire-and-gather sobre los primeros K)
    ranked = await _attach_geometry(ranked, snapped_p, snapped_d, osrm)

    # 7. Publicar match.shown (fire-and-forget)
    asyncio.create_task(_publish_match_shown(req, ranked, redis))

    # 8. Log estructurado
    logger.info("match | requestId=%s candidates=%d viable=%d returned=%d elapsed_ms=%.1f",
                req.requestId, len(rides), len(viable), len(ranked),
                (time.monotonic()-t0)*1000)

    return MatchResponse(requestId=req.requestId,
                         modelVersion=ranker.model_version,
                         candidates=ranked)
```

---

## 10. `GET /health`

Liveness sin tocar DB ni OSRM → `200 {"status":"ok"}`. Lo exige el healthCheck de `infra/task-def-v2.json`. Ese healthCheck usa `curl`, que **no existe** en `python:3.13-slim`: cambiar el comando a
`python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)"`.

## 10.1 `main.py` — convivencia HTTP + consumer

```python
async def _run():
    # Recursos compartidos
    db_pool   = await asyncpg.create_pool(...)
    redis     = aioredis.from_url(...)
    osrm      = OsrmClient(settings.OSRM_URL)

    # FastAPI app con el router de matching
    app = FastAPI()
    app.include_router(matching_router)

    # Servidor HTTP (uvicorn) + consumer Redis Streams en el mismo event loop
    config = uvicorn.Config(app, host="0.0.0.0", port=settings.HTTP_PORT, loop="none")
    server = uvicorn.Server(config)

    await asyncio.gather(
        server.serve(),
        run_consumer(redis, ...),
    )
```

---

## 11. Nuevas variables de entorno

| Variable | Default | Descripción |
|----------|---------|-------------|
| `OSRM_URL` | `http://osrm:5000` | URL base del motor de rutas |
| `OSRM_MAX_TABLE_SIZE` | `100` | Máx. coordenadas por llamada a /table |
| `CANDIDATE_RADIUS_M` | `1500` | Radio PostGIS para candidatos (metros) |
| `MAX_CANDIDATES` | `80` | Máx. rides a evaluar por búsqueda |
| `ROUTE_GEOMETRY_TOP_K` | `3` | Primeros K candidatos con trazado GeoJSON |
| `PICKUP_ETA_NORM_S` | `1800` | Normalización de ETA en scoring |
| `SCORE_W_DETOUR` | `0.5` | Peso del factor desvío |
| `SCORE_W_TIME` | `0.3` | Peso del factor horario |
| `SCORE_W_ETA` | `0.2` | Peso del factor ETA |
| `HTTP_PORT` | `8000` | Puerto del servidor HTTP interno |
| `BBOX_LAT_MIN` | `17.8` | Bbox Yucatán — lat mínima |
| `BBOX_LAT_MAX` | `21.7` | Bbox Yucatán — lat máxima |
| `BBOX_LNG_MIN` | `-92.5` | Bbox Yucatán — lng mínima |
| `BBOX_LNG_MAX` | `-86.7` | Bbox Yucatán — lng máxima |

---

## 12. Nuevas dependencias

```toml
"fastapi>=0.115,<1"
"uvicorn[standard]>=0.30,<1"
"httpx>=0.27,<1"
# dev: "respx"
```

Sin `shapely`: `GET /route/v1/driving/A;P;D;B?overview=full&geometries=geojson` ya devuelve el GeoJSON LineString completo.

El `Dockerfile` pasa a instalar desde `pyproject.toml` (hoy tiene la lista de dependencias duplicada a mano) y añade `EXPOSE 8000`.

---

## 13. Fixture de test `tests/fixtures/crud_ts_schema.sql`

**No es una migración**: no vive en `migrations/` y nunca se aplica fuera de tests. Es una réplica mínima del esquema de `crud-ts` para poder probar la query de candidatos contra PostGIS real; si `crud-ts` cambia su esquema, esta réplica se actualiza para reflejarlo (la fuente de verdad sigue siendo `crud-ts`).

```sql
CREATE EXTENSION IF NOT EXISTS postgis;

-- Tabla rides (dueño: crud-ts; réplica solo para tests)
CREATE TABLE IF NOT EXISTS rides (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    driver_id        UUID NOT NULL,
    origin           GEOMETRY(Point, 4326) NOT NULL,
    destination      GEOMETRY(Point, 4326) NOT NULL,
    route            GEOMETRY(LineString, 4326) NOT NULL,
    duration_s       INTEGER NOT NULL,
    distance_m       INTEGER NOT NULL,
    departure_at     TIMESTAMPTZ NOT NULL,
    seats_available  SMALLINT NOT NULL DEFAULT 1,
    max_detour_s     INTEGER NOT NULL DEFAULT 300,
    status           TEXT NOT NULL DEFAULT 'scheduled'
);

CREATE INDEX IF NOT EXISTS rides_route_gix ON rides USING GIST(route);
CREATE INDEX IF NOT EXISTS rides_departure  ON rides(departure_at);
CREATE INDEX IF NOT EXISTS rides_status     ON rides(status);

-- Tabla matches (dueño: crud-ts; aquí solo para tests locales)
CREATE TABLE IF NOT EXISTS matches (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ride_id      UUID NOT NULL,
    passenger_id UUID NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
);

-- Tabla user_preferences (dueño: crud-ts; aquí solo para tests locales)
CREATE TABLE IF NOT EXISTS user_preferences (
    user_id             UUID PRIMARY KEY,
    raw_text            TEXT,
    revision            INTEGER NOT NULL DEFAULT 0,
    structured          JSONB,
    structured_revision INTEGER,
    structured_at       TIMESTAMPTZ
);
```

---

## 14. Tests

| Archivo | Casos principales |
|---------|-------------------|
| `test_candidates.py` | Ride en radio → aparece; sentido inverso → no aparece; match activo → no aparece; sin asientos → no aparece |
| `test_detour.py` | Construcción correcta de índices de la matriz; celdas null → descarte; desvío negativo → 0 en score |
| `test_scoring.py` | Pesos configurables; ordenamiento correcto; desempate determinista |
| `test_router.py` | Happy path end-to-end con mocks de OSRM y DB; bbox inválido → 422; OSRM caído → 503; lista vacía → 200 |
| `test_osrm_client.py` | `nearest` / `table` / `route` con `respx` mock; celdas null; error si se excede `OSRM_MAX_TABLE_SIZE`; reintento ante NetworkError |

`test_detour.py` cubre además la partición en lotes: con `n` > `OSRM_MAX_TABLE_SIZE − 2` candidatos (p. ej. tamaño 10 y 20 candidatos), cada lote incluye P y D y el resultado es idéntico al de una sola matriz.
