# Requirements — slice `matching` con OSRM

## 1. Restricciones del sistema

| ID | Restricción |
|----|-------------|
| C-01 | `matching-py` es el único servicio que ejecuta la lógica de matching. `crud-ts` solo reenvía la búsqueda y muestra el resultado. |
| C-02 | Este slice **nunca** invoca Bedrock ni al `LLMProvider`. Solo lee `user_preferences.structured` que el slice `preferences` ya dejó listo. |
| C-03 | Las tablas `rides`, `matches` y `user_preferences` son propiedad de `crud-ts` — solo lectura aquí. |
| C-04 | El motor de rutas (OSRM, perfil `driving`, grafo Yucatán) corre en la red interna como servicio separado; este slice solo lo consume por HTTP. |
| C-05 | La llamada desde `crud-ts` es de confianza (red interna); no se añade autenticación. |
| C-06 | `POST /match` convive en el mismo proceso que el consumer de Redis Streams del slice `preferences`. Ambos comparten el event loop asyncio. |
| C-07 | La ruta original del ride (`route`, `duration_s`, `distance_m`) la crea y mantiene `crud-ts` al publicar el ride. Este slice NO la crea, recalcula ni persiste; solo calcula al vuelo (sin guardar) el recorrido con pasajero A→P→D→B. |
| C-08 | Este slice NO incluye migraciones sobre `rides`, `matches` ni `user_preferences`. Si los tests necesitan esas tablas, se crean con fixtures de test (fuera de `migrations/`). |

---

## 2. Funcionales

### RF-01 — Endpoint `POST /match`
El servicio DEBE exponer `POST /match` con el siguiente contrato (inmutable sin versionado):

**Request:**
```json
{
  "requestId":    "uuid",
  "passengerId":  "uuid",
  "pickup":       { "lat": 21.0, "lng": -89.6 },
  "dropoff":      { "lat": 21.0, "lng": -89.6 },
  "departureAt":  "ISO-8601",
  "timeWindowMin": 30,
  "limit":        10
}
```

**Response 200:**
```json
{
  "requestId":    "uuid",
  "modelVersion": "heuristic-v1",
  "candidates": [{
    "rideId":        "uuid",
    "rank":          1,
    "score":         0.87,
    "detourS":       240,
    "pickupEtaS":    420,
    "snappedPickup":  { "lat": 0.0, "lng": 0.0 },
    "snappedDropoff": { "lat": 0.0, "lng": 0.0 },
    "routeGeometry": null
  }]
}
```

### RF-02 — Validación geográfica
El sistema DEBE rechazar con **422** cualquier búsqueda cuyo `pickup` o `dropoff` esté fuera del rectángulo de la península de Yucatán:
`lat ∈ [17.8, 21.7]`, `lng ∈ [-92.5, -86.7]`.

### RF-03 — Respuesta vacía
Si ningún ride cumple los criterios, el sistema DEBE responder 200 con `candidates: []`. Nunca un error.

### RF-04 — Error 503 ante caída de OSRM
Si OSRM no responde tras un reintento, el sistema DEBE responder **503** (no lista vacía).

### RF-05 — Selección de candidatos
Solo son candidatos los rides que cumplan **todos**:
- `status = 'scheduled'`
- `seats_available > 0`
- `departure_at` dentro de `±timeWindowMin` respecto a `departureAt` del request
- `driver_id ≠ passengerId`
- El pasajero no tiene match activo (estado no terminal) con ese ride
- La ruta del ride pasa a ≤ `CANDIDATE_RADIUS_M` (default 1 500 m) tanto del pickup **como** del dropoff (PostGIS `ST_DWithin`)
- El punto más cercano al pickup en la ruta ocurre **antes** que el más cercano al dropoff (`ST_LineLocatePoint`), garantizando sentido correcto

### RF-06 — Límite de candidatos
Se evalúan como máximo `MAX_CANDIDATES` (default 80) rides, priorizando los de menor suma de distancias corredor→pickup + corredor→dropoff.

### RF-07 — Interfaz `CandidateSource` intercambiable
La fuente de candidatos DEBE estar detrás de un protocolo / interfaz para poder cambiar de PostGIS a H3 sin modificar el handler.

### RF-08 — Snap a calles
Antes de calcular desvíos, pickup y dropoff DEBEN ajustarse al punto transitable más cercano del grafo vial (`OSRM /nearest`). Los puntos ajustados son los que se devuelven en `snappedPickup` / `snappedDropoff` y los que se usan en todos los cálculos.

### RF-09 — Cálculo de desvío exacto
Para cada candidato el desvío DEBE calcularse como:

```
detour_i = t(Aᵢ→P) + t(P→D) + t(D→Bᵢ) − ride.duration_s
```

donde `Aᵢ` = origen del conductor `i`, `Bᵢ` = destino, `P` = pickup ajustado, `D` = dropoff ajustado.

### RF-10 — Una sola llamada de matriz OSRM
Todos los tiempos DEBEN obtenerse en **una sola llamada** a `OSRM /table` por búsqueda, con coordenadas `[A₁…Aₙ, P, D, B₁…Bₙ]`. Solo si `(n+2)² > OSRM_MAX_TABLE_SIZE²` (default 100 → n > 98) se reparte en lotes con P y D en cada uno; el resultado debe ser idéntico (design §4.3).

### RF-11 — Descarte por desvío
Un candidato DEBE descartarse si:
- Alguna celda de la matriz es `null` (sin ruta posible), **o**
- `detour_i > ride.max_detour_s`

### RF-12 — Filtro duro de preferencias
Si `user_preferences.structured.max_detour_minutes` no es nulo, se descarta todo candidato con `detourS > max_detour_minutes × 60`. Aplica el límite más restrictivo entre el del conductor y el del pasajero.

### RF-13 — Resto de preferencias como features (no filtro)
`allow_pets`, `allow_smoking`, `allow_music`, `preferred_areas`, `avoided_areas`, `time_windows`, `avoid_strangers_at_night` solo se registran como características en `match.shown` para el ranker futuro. En `heuristic-v1` no modifican el score.

### RF-14 — `gender_preference` ignorado
Se ignora en esta entrega. Los datos del conductor necesarios no están disponibles para este slice.

### RF-15 — Scoring `heuristic-v1`
Cada candidato viable recibe un score `∈ [0, 1]`:

```
score = w_detour·(1 − detour_n) + w_time·(1 − time_n) + w_eta·(1 − eta_n)
```

Donde (cada factor recortado a `[0, 1]`):
- `detour_n = max(detourS, 0) / límite_efectivo_s`
- `time_n   = |hora_salida_ride − departureAt| / (timeWindowMin × 60)`
- `eta_n    = pickupEtaS / PICKUP_ETA_NORM_S` (default 1 800 s)

Pesos por defecto: `w_detour=0.5`, `w_time=0.3`, `w_eta=0.2`. DEBEN ser configurables por variables de entorno.

### RF-16 — Ordenamiento y `rank`
Los candidatos DEBEN ordenarse por `score` descendente (desempate: menor `detourS`, luego `rideId` lexicográfico). Se numeran con `rank` desde 1 y se recortan a `limit`.

### RF-17 — `RankingStrategy` intercambiable
La estrategia de ordenamiento DEBE estar detrás de un protocolo para sustituirla por `lgbm-vN` sin tocar el handler. La respuesta reporta en `modelVersion` cuál se usó.

### RF-18 — Trazado (`routeGeometry`)
Solo los primeros `ROUTE_GEOMETRY_TOP_K` (default 3) candidatos incluyen `routeGeometry`: GeoJSON LineString del recorrido completo `Aᵢ → P → D → Bᵢ`. El resto llevan `null`.

### RF-19 — Evento `match.shown`
Por cada búsqueda con al menos un candidato, el sistema DEBE publicar en `rides.events` (campo `envelope`, XADD) el evento:

```json
{
  "id": "uuid",
  "type": "match.shown",
  "version": 1,
  "source": "matching-py",
  "aggregateType": "match_request",
  "aggregateId": "<requestId>",
  "occurredAt": "ISO-8601",
  "data": {
    "requestId": "uuid",
    "passengerId": "uuid",
    "modelVersion": "heuristic-v1",
    "departureAt": "ISO-8601",
    "timeWindowMin": 30,
    "pickup":  { "lat": 0.0, "lng": 0.0 },
    "dropoff": { "lat": 0.0, "lng": 0.0 },
    "candidates": [
      { "rideId": "uuid", "rank": 1, "score": 0.87, "detourS": 240, "pickupEtaS": 420, "features": {} }
    ]
  }
}
```

### RF-20 — `match.shown` no bloquea la respuesta
La publicación DEBE ser fire-and-forget. Un fallo de publicación se registra en logs y se descarta; la respuesta al pasajero se entrega igual.

### RF-21 — Observabilidad
Cada búsqueda DEBE registrar en logs estructurados (JSON): `requestId`, número de candidatos generados, descartados por motivo (sin ruta, desvío excedido, preferencias), devueltos, y tiempo empleado en cada fase (candidatos, snap, matriz, trazado).

---

## 3. No funcionales

| ID | Requisito |
|----|-----------|
| NF-01 | p95 de latencia < 400 ms con 80 candidatos a evaluar. |
| NF-02 | El cliente OSRM usa `httpx.AsyncClient` reutilizable con `connect_timeout=1s`, `read_timeout=2s` (configurables) y un reintento ante errores de red; tras eso, 503. Timeouts mayores romperían el presupuesto de 400 ms. |
| NF-06 | `GET /health` (liveness, sin dependencias) para el healthCheck de ECS. |
| NF-03 | El servidor HTTP y el consumer de Redis Streams corren en el mismo event loop asyncio (mismo proceso). |
| NF-04 | Los pesos de `heuristic-v1` y todos los umbrales DEBEN ser configurables por variables de entorno sin cambiar código. |
| NF-05 | La estrategia de candidatos y la de ranking DEBEN ser intercambiables por inyección de dependencias. |

---

## 4. Fuera de alcance

- Ranker aprendido LightGBM (`lgbm-vN`) y su entrenamiento.
- `gender_preference` como filtro o factor de score.
- Autenticación del endpoint `POST /match`.
- Escritura en ninguna tabla de la base de datos.
- Índice hexagonal H3 (la interfaz `CandidateSource` lo prepara, pero solo se implementa PostGIS).
- Crear, recalcular o persistir la ruta del ride, y cualquier DDL sobre tablas de `crud-ts` (C-07, C-08).
- KNN por embeddings de preferencias (`knn_by_preferences`) y script de simulación de eventos: no forman parte del contrato de `POST /match`; si se necesitan, van en una spec propia del ranker / tooling.
