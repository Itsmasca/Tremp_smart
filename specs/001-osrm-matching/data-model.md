# Data Model — Slice `matching` con OSRM

**Feature**: [spec.md](./spec.md) · **Fecha**: 2026-10-24

Este slice **no persiste nada**. Todo lo que sigue son (a) lecturas de tablas de crud-ts, (b) modelos en memoria por request y (c) el evento publicado.

---

## 1. Tablas leídas (propiedad de crud-ts, solo lectura)

### 1.1 `rides`

| Columna | Tipo | Uso en este slice |
|---------|------|-------------------|
| `id` | UUID | `rideId` |
| `driver_id` | UUID | excluir rides del propio pasajero |
| `origin` | geometry(Point, 4326) | `A` (origen del conductor) |
| `destination` | geometry(Point, 4326) | `B` (destino del conductor) |
| `route` | geometry(LineString, 4326) | filtro por corredor y dirección. **Generada por crud-ts; nunca se recalcula aquí** |
| `duration_s` | int | duración original (resta en el desvío) |
| `distance_m` | int | feature |
| `departure_at` | timestamptz | ventana horaria, `time_n` |
| `seats_available` | int | `> 0` |
| `max_detour_s` | int | límite del conductor |
| `status` | text | `= 'scheduled'` |

Índices que se esperan de crud-ts (ver research R-06): GIST en `route`, B-tree en `departure_at` (ideal parcial por `status='scheduled'`).

### 1.2 `matches`

| Columna | Uso |
|---------|-----|
| `ride_id`, `passenger_id` | `NOT EXISTS` match activo |
| `status` | activo = no está en `MATCH_TERMINAL_STATUSES` (default `rejected, completed, cancelled`) |

### 1.3 `user_preferences`

| Columna | Uso |
|---------|-----|
| `user_id` | = `passengerId` |
| `structured` (jsonb) | validado como `StructuredPreferences` v1; nulo o inválido → sin preferencias |

---

## 2. Modelos de request/response (contrato con crud-ts)

Definición exacta en [contracts/match-api.openapi.yaml](./contracts/match-api.openapi.yaml).

### `MatchRequest`

| Campo | Tipo | Validación |
|-------|------|-----------|
| `requestId` | UUID | requerido |
| `passengerId` | UUID | requerido |
| `pickup`, `dropoff` | `LatLng` | requeridos; dentro del bbox `BBOX_*` → si no, 422 |
| `departureAt` | datetime ISO-8601 con zona | requerido |
| `timeWindowMin` | int | default 30, `≥ 0` |
| `limit` | int | default 10, `≥ 1` |

### `MatchResponse` / `Candidate`

| Campo | Tipo | Regla |
|-------|------|-------|
| `requestId` | UUID | eco |
| `modelVersion` | str | `RankingStrategy.model_version` (`heuristic-v1`) |
| `candidates[].rideId` | UUID | |
| `candidates[].rank` | int | 1..len, consecutivo |
| `candidates[].score` | float | `[0, 1]` |
| `candidates[].detourS` | int | desvío real redondeado (puede ser negativo; ver §3) |
| `candidates[].pickupEtaS` | int | `t(A→P)` |
| `candidates[].snappedPickup/Dropoff` | `LatLng` | iguales para todos los candidatos de una respuesta |
| `candidates[].routeGeometry` | GeoJSON LineString \| null | no nulo sólo para `rank ≤ ROUTE_GEOMETRY_TOP_K` (salvo fallo parcial de `/route`) |

> Nota: `detourS` se devuelve con el valor real (puede ser < 0 por diferencias de grafo); filtro y score usan `max(detourS, 0)`.

---

## 3. Modelos internos (por request, en memoria)

```
LatLng(lat, lng)                                   # app/shared/osrm/types.py
SnappedPoint(location: LatLng, distance_m)         # resultado de /nearest
Matrix(durations[i][j]: float|None, distances[i][j]: float|None)
Route(duration_s, distance_m, geometry: dict)

RideRow                                            # fila de rides ya filtrada por SQL
  id, driver_id, origin: LatLng, destination: LatLng,
  duration_s, distance_m, departure_at, seats_available,
  max_detour_s, corridor_dist_m

DetourResult                                       # salida de detour.py (viables)
  ride: RideRow
  t_ap_s, t_pd_s, t_db_s           # tramos
  d_ap_m, d_pd_m, d_db_m           # distancias
  detour_s_raw                     # puede ser < 0
  detour_limit_s                   # min(max_detour_s, prefs.max_detour_minutes*60)

ScoredCandidate
  detour: DetourResult
  score: float
  features: dict                   # ver research R-10
  rank: int | None                 # asignado tras ordenar
  route_geometry: dict | None
```

### Reglas de descarte (en orden, con motivo para logs)

| Motivo | Condición |
|--------|-----------|
| `no_route` | alguna de `t(A→P)`, `t(P→D)`, `t(D→B)` es `null` |
| `detour_driver` | `max(detour_raw,0) > ride.max_detour_s` |
| `detour_passenger` | `prefs.max_detour_minutes` no nulo y `max(detour_raw,0) > max_detour_minutes·60` |

Si `t(P→D)` es `null` → todos se descartan como `no_route` (respuesta vacía, no error).

### Puntuación (`heuristic-v1`)

```
detour_n = clip(max(detour_raw,0) / detour_limit_s)        # detour_limit_s = 0 → detour_n = 0 si detour ≤ 0
time_n   = clip(|ride.departure_at − departureAt|.total_seconds() / (timeWindowMin·60))   # timeWindowMin = 0 → 0
eta_n    = clip(t_ap_s / PICKUP_ETA_NORM_S)
score    = clip(W_DETOUR·(1−detour_n) + W_TIME·(1−time_n) + W_ETA·(1−eta_n))
orden    = score DESC, max(detour_raw,0) ASC, rideId ASC
```

---

## 4. Evento publicado: `match.shown`

Esquema en [contracts/match-shown.event.schema.json](./contracts/match-shown.event.schema.json).

- Stream: `REDIS_STREAM` (`rides.events`), campo `envelope`.
- Uno por búsqueda con ≥ 1 candidato. No se emite con lista vacía.
- Inmutable; los consumidores deduplican por `data.requestId` (crud-ts puede reintentar).

---

## 5. Configuración nueva (`Settings`)

| Variable | Default | Validación |
|----------|---------|-----------|
| `HTTP_PORT` | 8000 | |
| `OSRM_URL` | `http://osrm:5000` | |
| `OSRM_MAX_TABLE_SIZE` | 100 | `≥ 3`; = `--max-table-size` del contenedor |
| `OSRM_CONNECT_TIMEOUT_S` / `OSRM_READ_TIMEOUT_S` | 1.0 / 2.0 | |
| `CANDIDATE_RADIUS_M` | 1500 | `> 0` |
| `MAX_CANDIDATES` | 80 | `≥ 1` |
| `ROUTE_GEOMETRY_TOP_K` | 3 | `≥ 0` |
| `PICKUP_ETA_NORM_S` | 1800 | `> 0` |
| `SCORE_W_DETOUR` / `SCORE_W_TIME` / `SCORE_W_ETA` | 0.5 / 0.3 / 0.2 | `≥ 0`, suma `> 0` |
| `BBOX_LAT_MIN/MAX`, `BBOX_LNG_MIN/MAX` | 17.8 / 21.7 / −92.5 / −86.7 | min < max |
| `MATCH_TERMINAL_STATUSES` | `rejected,completed,cancelled` | lista |
| `DB_POOL_MAX_SIZE` | 10 | compartido con el consumer |
