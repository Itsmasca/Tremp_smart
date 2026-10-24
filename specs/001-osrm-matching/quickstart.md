# Quickstart — validar el slice `matching`

Guía para comprobar de punta a punta que la feature funciona. Contratos: [match-api.openapi.yaml](./contracts/match-api.openapi.yaml), [match-shown.event.schema.json](./contracts/match-shown.event.schema.json). Modelo: [data-model.md](./data-model.md).

## 0. Prerrequisitos

- Docker, `uv`, Python 3.13.
- Extracto OSM de Yucatán (p. ej. `mexico-latest.osm.pbf` recortado a la península) o una imagen OSRM ya preprocesada del equipo.
- Puertos libres: 5432 (Postgres), 6379 (Redis), 5000 (OSRM), 8000 (matching-py).

## 1. Infra local

```bash
# Postgres + PostGIS + pgvector
docker run -d --name pg -p 5432:5432 -e POSTGRES_PASSWORD=pass postgis/postgis:16-3.4
# Redis
docker run -d --name redis -p 6379:6379 redis:7
# OSRM (grafo ya preprocesado con osrm-extract/partition/customize, perfil car)
docker run -d --name osrm -p 5000:5000 -v "$PWD/osrm-data:/data" \
  ghcr.io/project-osrm/osrm-backend osrm-routed --algorithm mld --max-table-size 100 /data/yucatan.osrm
```

Esquema de crud-ts **solo para pruebas** (no es una migración del servicio):

```bash
psql postgresql://postgres:pass@localhost:5432/postgres -f tests/fixtures/crud_ts_schema.sql
psql postgresql://postgres:pass@localhost:5432/postgres -f migrations/001_user_preference_embeddings.sql
```

## 2. Arrancar el servicio

```bash
uv sync --extra dev
export DATABASE_URL=postgresql://postgres:pass@localhost:5432/postgres
export REDIS_URL=redis://localhost:6379/0
export OSRM_URL=http://localhost:5000
export LLM_PROVIDER=fake
uv run python main.py      # levanta HTTP :8000 + consumer de preferences
curl -s localhost:8000/health   # → {"status":"ok"}
```

## 3. Escenarios de validación

Sembrar rides (los datos de ejemplo viven en el fixture de tests de integración). Rutas sugeridas en Mérida, generadas con `GET /route` del OSRM local para que `duration_s` sea coherente:

| Ride | Recorrido del conductor | Esperado para pickup Altabrisa → dropoff Centro |
|------|-------------------------|--------------------------------------------------|
| R1 | Cholul → Centro, sale 07:35, 2 asientos, `max_detour_s=600` | **aparece** |
| R2 | Centro → Cholul (sentido contrario) | no aparece (dirección) |
| R3 | igual a R1 pero `seats_available=0` | no aparece |
| R4 | igual a R1 pero sale 09:00 | no aparece (ventana ±30) |
| R5 | igual a R1 pero `max_detour_s=30` | no aparece (desvío) |
| R6 | igual a R1, `driver_id = passengerId` | no aparece (propio) |
| R7 | igual a R1 con match `pending` del pasajero | no aparece (match activo) |

### 3.1 Happy path (US1 / US2)

```bash
curl -s localhost:8000/match -H 'content-type: application/json' -d '{
  "requestId":"7b1c2f0e-3d4a-4c8b-9a51-2f1e0c9d8a77",
  "passengerId":"0f8e7d6c-5b4a-4392-8170-6f5e4d3c2b1a",
  "pickup":{"lat":21.0186,"lng":-89.5847},
  "dropoff":{"lat":20.9671,"lng":-89.6237},
  "departureAt":"2026-10-26T07:30:00-06:00"
}' | jq
```

Esperado: sólo R1, `rank=1`, `0 ≤ score ≤ 1`, `routeGeometry` no nulo, `snappedPickup` ≠ pickup original si estaba fuera de calle.

**Verificar el desvío (SC-003)**: llamar `GET /route/v1/driving/{A};{P'};{D'};{B}` con los puntos ajustados devueltos; `duration − ride.duration_s` debe diferir de `detourS` en < 1 s.

### 3.2 Evento `match.shown` (US3)

```bash
redis-cli XREVRANGE rides.events + - COUNT 1
```

Esperado: un mensaje con campo `envelope`, `type="match.shown"`, `aggregateId = requestId`, candidatos = los de la respuesta. Valida contra el JSON Schema.

Con Redis detenido (`docker stop redis`), repetir 3.1 → la respuesta sigue siendo 200 y el log muestra el fallo de publicación.

### 3.3 Errores (US4)

| Caso | Acción | Esperado |
|------|--------|----------|
| Fuera de bbox | `pickup: {lat: 25.6, lng: -100.3}` (Monterrey) | 422, `loc=["body","pickup"]` |
| Request inválido | `departureAt: "mañana"` | 422 |
| OSRM caído | `docker stop osrm` y repetir 3.1 | 503 (no `[]`) |
| Sin candidatos | pickup/dropoff en Progreso sin rides | 200 `candidates: []`, sin evento |

### 3.4 Preferencias

```sql
INSERT INTO user_preferences (user_id, structured)
VALUES ('0f8e7d6c-5b4a-4392-8170-6f5e4d3c2b1a', '{"schema_version":1,"max_detour_minutes":1,"confidence":0.9}');
```

Repetir 3.1: si el desvío de R1 > 60 s, R1 desaparece (log con motivo `detour_passenger`).

## 4. Pruebas automatizadas

```bash
uv run pytest tests/unit                    # sin red: respx + fakes
uv run pytest -m integration                # requiere el Postgres del paso 1
```

## 5. Verificar el límite de tabla de OSRM

Confirma la regla de research R-03 (`sources × destinations ≤ max_table_size²`) con la imagen que se despliega:

```bash
# 82 sources × 82 destinations con --max-table-size 100 → debe responder code=Ok
uv run python -m scripts.check_osrm_table_limit --n 80   # helper de diagnóstico a crear en tasks
```

Si responde `TooBig`, el contenedor aplica la regla por número de coordenadas: cambiar `max_batch_candidates()` según research R-03.

## 6. Rendimiento (SC-001)

Con 80 rides sembrados cerca del corredor Altabrisa→Centro, lanzar 200 búsquedas (p. ej. con `hey -n 200 -c 10 -m POST -D body.json localhost:8000/match`) y comprobar **p95 < 400 ms**. Revisar en los logs el tiempo por fase si no se cumple. Confirmar con `EXPLAIN ANALYZE` que la query de candidatos usa el índice GIST de `rides.route`.
