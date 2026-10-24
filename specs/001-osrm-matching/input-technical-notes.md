# Notas técnicas de entrada — slice `matching` con OSRM

Texto original del usuario, preservado tal cual para `/speckit-plan` (la spec se mantiene a nivel de QUÉ; el CÓMO vive aquí).

> ⚠️ El texto llegó **truncado** al final de la sección "Cálculo de desvío" ("Guardar también"). Lo que siga (ranking, evento `match.shown`, preferencias, tests, etc.) falta.

## Contexto
Plataforma de rides comunitarios (Mérida, Yucatán).
- `crud-ts` (NestJS): única API que ve la app móvil. Reenvía las búsquedas a este servicio por HTTP interno.
- `matching-py` (este servicio): genera candidatos, calcula desvíos con OSRM y los ordena.
- OSRM: contenedor aparte en la red interna (`OSRM_URL`), grafo de Yucatán, perfil `driving`. Este servicio solo lo consume por HTTP.
- Postgres + PostGIS compartido. Las tablas `rides`, `matches` y `user_preferences` las crea crud-ts; aquí son SOLO LECTURA.
- Redis Streams `rides.events` para eventos.
- El slice `preferences` (Bedrock) ya existe o está en otra spec. Este slice solo LEE `user_preferences.structured`; nunca llama a Bedrock.

## Objetivo
Implementar `POST /match`: dado el origen, destino y hora de un pasajero, devolver los rides viables ordenados por relevancia, con desvío exacto calculado con OSRM, y emitir `match.shown` para entrenar el ranker.

## Contrato HTTP
(lo consume crud-ts; no cambiarlo sin versionar)

Request:
```json
{
  "requestId": "uuid",
  "passengerId": "uuid",
  "pickup":  { "lat": 21.0, "lng": -89.6 },
  "dropoff": { "lat": 21.0, "lng": -89.6 },
  "departureAt": "ISO-8601",
  "timeWindowMin": 30,
  "limit": 10
}
```
Response 200:
```json
{
  "requestId": "uuid",
  "modelVersion": "heuristic-v1 | lgbm-vN",
  "candidates": [
    {
      "rideId": "uuid",
      "rank": 1,
      "score": 0.87,
      "detourS": 240,
      "pickupEtaS": 420,
      "snappedPickup":  { "lat": 0, "lng": 0 },
      "snappedDropoff": { "lat": 0, "lng": 0 },
      "routeGeometry": null
    }
  ]
}
```
- `routeGeometry` (GeoJSON LineString del recorrido A→P→D→B) solo para el top `ROUTE_GEOMETRY_TOP_K` (default 3); `null` para el resto.
- Lista vacía si no hay candidatos (no es error).
- 422 si las coordenadas están fuera del bbox de la península; 503 si OSRM no responde.
- Presupuesto de latencia p95 < 400 ms con 80 candidatos.

Columnas que se leen de `rides`: `id, driver_id, origin, destination, route (geometry LineString 4326), duration_s, distance_m, departure_at, seats_available, max_detour_s, status`.

## Requisitos

### Cliente OSRM (`shared/osrm/`)
- Cliente async con `httpx.AsyncClient` reutilizable, timeouts y un reintento ante errores de red.
- Métodos:
  - `nearest(point) -> SnappedPoint`
  - `table(coords, sources, destinations) -> Matrix(durations, distances)`
  - `route(coords) -> Route(duration, distance, geometry)`
- Orden de coordenadas **lon,lat** en la URL. Encapsularlo para que el resto del código use `LatLng(lat, lng)`.
- Manejar `code != "Ok"` y celdas `null` (sin ruta posible) en la matriz.
- Partir en lotes si `len(coords)` supera `OSRM_MAX_TABLE_SIZE` (env, default 100; documentar que debe coincidir con el `--max-table-size` del contenedor).

### Generación de candidatos (`features/matching/candidates.py`)
- Query PostGIS: rides `scheduled`, `seats_available > 0`, `departure_at` dentro de ±`timeWindowMin`, sin match activo del mismo pasajero y con `driver_id != passengerId`.
- Filtro espacial: `ST_DWithin(route::geography, pickup, R)` Y `ST_DWithin(route::geography, dropoff, R)`, con `R = CANDIDATE_RADIUS_M` (default 1500).
- Validar dirección: la proyección del pickup sobre la ruta (`ST_LineLocatePoint`) debe ser menor que la del dropoff.
- Tope de `MAX_CANDIDATES` (default 80) ordenados por suma de distancias al corredor.
- Dejar una interfaz `CandidateSource` para cambiar a índice H3 después sin tocar el handler.

### Cálculo de desvío (`features/matching/detour.py`)
- Snap de pickup y dropoff con `nearest`.
- Una sola llamada a `table` con coordenadas `[A1..An, P, D, B1..Bn]`, `sources=[A1..An, P, D]` y `destinations=[P, D, B1..Bn]`.
- `detour_i = t(Ai→P) + t(P→D) + t(D→Bi) − ride.duration_s`.
- Descartar si hay alguna celda `null` o si `detour_i > ride.max_detour_s`.
- Guardar también *(texto truncado aquí)*

## Notas de la revisión de specs Kiro (para el plan)

- Reutilizar `app/shared/config.py` (`Settings`, pydantic-settings) añadiendo `OSRM_URL`, `OSRM_MAX_TABLE_SIZE`, `CANDIDATE_RADIUS_M`, `MAX_CANDIDATES`, `ROUTE_GEOMETRY_TOP_K`, bbox.
- Reutilizar el pool `asyncpg` y el cliente Redis ya creados en `main.py`; hoy `main.py` solo corre `run_consumer` → hay que levantar el servidor HTTP y el consumer en el mismo event loop (o procesos separados).
- Envelope de eventos: mismo formato que `PreferencesUpdatedEnvelope` (`app/shared/events/envelope.py`), publicado con `XADD` bajo el campo `envelope` como hace el consumer.
- `StructuredPreferences` (v1) vive en `app/features/preferences/schema.py`: importarlo para leer `structured`, sin acoplar el slice matching al LLM.
- `pyproject.toml` aún no tiene framework HTTP, `httpx`, ni dependencias de geometría; añadirlos en el plan.
- Seguir la estructura vertical-slice existente: `app/features/matching/`, `app/shared/osrm/`, tests en `tests/unit/matching/` y `tests/unit/shared/`.
