# matching-py

Servicio Python del sistema de rides comunitarios (Mérida, Yucatán).  
Responsabilidades: candidatos PostGIS (H3 a futuro), desvíos OSRM, ranking (`heuristic-v1`; LightGBM a futuro), extracción de preferencias con Bedrock.

---

## Arquitectura del slice `preferences`

```
Redis Streams (rides.events)
        │
        ▼  XREADGROUP (grupo: matching-preferences)
  consumer.py  ──dispatch──►  handler.py
                                  │
                         ┌────────┴────────┐
                         ▼                 ▼
                   LLMProvider        repository.py
                  (Protocol)               │
               ┌──────────┐    ┌──────────┴───────────┐
               │ Bedrock  │    │  user_preferences     │
               │ Provider │    │  (structured cols)    │
               └──────────┘    │                       │
                               │  user_preference_     │
                               │    embeddings         │
                               └───────────────────────┘
```

**Bedrock nunca queda en el camino síncrono de `POST /match`.** Solo se invoca al consumir eventos del stream.

---

## Slice `matching` — `POST /match`

Endpoint interno que consume **solo crud-ts**. Spec, plan y contratos: [`specs/001-osrm-matching/`](specs/001-osrm-matching/).

```
crud-ts ──POST /match──► router.py
                           │ 1. bbox Yucatán (422)
                           │ 2. en paralelo:
                           │      candidates.py ── 1 query PostGIS (rides/matches, solo lectura)
                           │      preferences.py ─ user_preferences.structured (solo lectura)
                           │      OSRM /nearest ×2 (snap de pickup/dropoff)
                           │ 3. detour.py ── 1 llamada OSRM /table [A…,P,D,B…] → descartes
                           │ 4. scoring.py ── heuristic-v1 → orden → limit
                           │ 5. OSRM /route A→P→D→B para el top-K (routeGeometry)
                           │ 6. events.py ── XADD match.shown a rides.events (fire-and-forget)
                           ▼
                     200 {candidates} | 422 | 503 (OSRM caído)
```

- La ruta original del ride (`rides.route`, `duration_s`, `distance_m`) la crea **crud-ts**. Este slice solo la lee; el recorrido con pasajero se calcula al vuelo y no se guarda.
- `rides`, `matches` y `user_preferences` son de solo lectura; este servicio no tiene migraciones sobre ellas.
- El servidor HTTP y el consumer de preferences corren en el **mismo proceso y event loop** (`main.py`). Si cualquiera termina, el proceso sale con código ≠ 0 para que ECS lo reinicie.
- `GET /health` es liveness: no toca la DB ni OSRM.

### Riesgos de infraestructura conocidos

- `OSRM_URL=http://osrm:5000` en Fargate (`awsvpc`) solo resuelve con ECS Service Connect o Cloud Map.
- `REDIS_CONSUMER_NAME` es fijo: al escalar a más de una tarea, dos instancias compartirían nombre de consumer.
- El rendimiento de la query de candidatos depende de que crud-ts tenga un índice GIST en `rides.route` y uno en `rides.departure_at`.
- `OSRM_MAX_TABLE_SIZE` debe coincidir con `--max-table-size` del contenedor OSRM. Verificar la regla de límite con `uv run python -m scripts.check_osrm_table_limit --n 80`.

---

## Variables de entorno

Copiar `.env.example` a `.env` y completar los valores.

| Variable | Requerida | Default | Descripción |
|----------|-----------|---------|-------------|
| `REDIS_URL` | ✅ | — | URL del broker Redis (ej: `redis://localhost:6379/0`) |
| `DATABASE_URL` | ✅ | — | DSN Postgres con pgvector (ej: `postgresql://user:pass@host:5432/db`) |
| `BEDROCK_REGION` | | `us-east-1` | Región AWS donde se invoca Bedrock |
| `BEDROCK_LLM_MODEL_ID` | | `us.anthropic.claude-3-5-haiku-20241022-v1:0` | Cross-region inference profile del LLM |
| `BEDROCK_EMBED_MODEL_ID` | | `amazon.titan-embed-text-v2:0` | Modelo de embeddings de Bedrock |
| `EMBED_DIM` | | `256` | Dimensión del vector (Titan V2: 256, 512 o 1024) |
| `LLM_PROVIDER` | | `bedrock` | `bedrock` \| `fake` — usar `fake` para desarrollo local sin AWS |
| `REDIS_STREAM` | | `rides.events` | Stream de entrada |
| `REDIS_DLQ_STREAM` | | `rides.events.dlq` | Stream dead-letter queue |
| `REDIS_CONSUMER_GROUP` | | `matching-preferences` | Nombre del consumer group |
| `REDIS_CONSUMER_NAME` | | `matching-py-1` | Nombre de esta instancia |
| `LOG_LEVEL` | | `INFO` | Nivel de log (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `DB_POOL_MAX_SIZE` | | `10` | Pool asyncpg compartido por consumer y HTTP |
| `HTTP_PORT` | | `8000` | Puerto del servidor HTTP interno |
| `OSRM_URL` | | `http://osrm:5000` | URL base de OSRM (perfil `driving`, grafo Yucatán) |
| `OSRM_MAX_TABLE_SIZE` | | `100` | Debe coincidir con `--max-table-size` del contenedor OSRM |
| `OSRM_CONNECT_TIMEOUT_S` / `OSRM_READ_TIMEOUT_S` | | `1.0` / `2.0` | Timeouts; tras 1 reintento → 503 |
| `CANDIDATE_RADIUS_M` | | `1500` | Distancia máxima de la ruta del ride a pickup y dropoff |
| `MAX_CANDIDATES` | | `80` | Rides evaluados por búsqueda |
| `ROUTE_GEOMETRY_TOP_K` | | `3` | Candidatos con `routeGeometry` |
| `MATCH_TERMINAL_STATUSES` | | `rejected,completed,cancelled` | Estados de `matches` que no cuentan como match activo |
| `PICKUP_ETA_NORM_S` | | `1800` | ETA a partir de la cual el factor ETA aporta 0 |
| `SCORE_W_DETOUR` / `SCORE_W_TIME` / `SCORE_W_ETA` | | `0.5` / `0.3` / `0.2` | Pesos de heuristic-v1 (se normalizan si no suman 1) |
| `BBOX_LAT_MIN` / `BBOX_LAT_MAX` / `BBOX_LNG_MIN` / `BBOX_LNG_MAX` | | `17.8` / `21.7` / `-92.5` / `-86.7` | Área de servicio; fuera → 422 |

---

## Ejecución local sin AWS

```bash
# Instalar dependencias
uv sync

# Configurar sin Bedrock
LLM_PROVIDER=fake REDIS_URL=redis://localhost:6379 DATABASE_URL=postgresql://... OSRM_URL=http://localhost:5000 python main.py

curl localhost:8000/health   # {"status":"ok"}
```

Guía completa para validar `POST /match` (OSRM local, rides de ejemplo en Mérida, errores, evento, p95): [`specs/001-osrm-matching/quickstart.md`](specs/001-osrm-matching/quickstart.md).

Con `LLM_PROVIDER=fake` no se necesitan credenciales AWS. El proveedor falso devuelve un objeto `StructuredPreferences` vacío (todos los campos en `null`) con `confidence=1.0`.

---

## Migración de base de datos

Requiere Postgres con la extensión `pgvector` instalada.

```bash
psql "$DATABASE_URL" -f migrations/001_user_preference_embeddings.sql
```

Si usas una dimensión diferente a 256, edita la línea `vector(256)` en la migración antes de ejecutarla.  
**Nota:** una vez creada la tabla, cambiar la dimensión requiere `DROP TABLE` + re-crear.

---

## Permisos IAM mínimos

La política siguiente concede solo lo necesario para que `matching-py` invoque los modelos de Bedrock.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BedrockInvokeModels",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel"],
      "Resource": [
        "arn:aws:bedrock:us-east-1::foundation-model/amazon.titan-embed-text-v2:0",
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-3-5-haiku-20241022-v1:0"
      ]
    },
    {
      "Sid": "BedrockInvokeInferenceProfile",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel"],
      "Resource": [
        "arn:aws:bedrock:us-east-1:*:inference-profile/us.anthropic.claude-3-5-haiku-20241022-v1:0"
      ]
    }
  ]
}
```

> El cross-region inference profile (`us.anthropic.claude-3-5-haiku-*`) requiere también permisos sobre el ARN del perfil en la cuenta. Sustituir `*` en el ARN por el ID de tu cuenta AWS.

---

## Tests

```bash
# Todos los tests (sin AWS real)
uv run pytest

# Solo el slice preferences
uv run pytest tests/unit/preferences/

# Solo el proveedor Bedrock (con Stubber)
uv run pytest tests/unit/shared/test_bedrock_provider.py

# Solo el slice matching (OSRM, Postgres y Redis falsos)
uv run pytest tests/unit/matching/ tests/unit/shared/test_osrm_client.py

# Integración: query de candidatos contra PostGIS real (excluida por defecto)
docker run -d --name pg -p 55432:5432 -e POSTGRES_PASSWORD=pass postgis/postgis:16-3.4
TEST_DATABASE_URL=postgresql://postgres:pass@localhost:55432/postgres uv run pytest -m integration
```

`tests/fixtures/crud_ts_schema.sql` es una **réplica de pruebas** del esquema de crud-ts que cargan los tests de integración. **No es una migración**: nunca se aplica fuera de los tests.

---

## Estructura del proyecto

```
app/
  features/
    preferences/
      handler.py        # Orquesta el evento preferences.updated
      schema.py         # StructuredPreferences (Pydantic v2)
      repository.py     # UPDATE condicional + upsert de embedding
      prompts.py        # System prompt en español (contexto Mérida)
    matching/
      router.py         # POST /match: orquestación, bbox (422), trazado top-K
      schema.py         # Contrato con crud-ts + modelos internos
      candidates.py     # CandidateSource + query PostGIS única (solo lectura)
      preferences.py    # Lee user_preferences.structured
      detour.py         # Matriz OSRM [A…,P,D,B…], lotes, descartes
      scoring.py        # RankingStrategy + heuristic-v1
      events.py         # match.shown fire-and-forget
  shared/
    http/
      app.py            # FastAPI: /health, 503 si OSRM no responde
    osrm/
      client.py         # OsrmClient (único lugar con orden lon,lat)
      types.py          # LatLng, SnappedPoint, Matrix, Route
      errors.py         # OsrmUnavailable, OsrmNoSegment, OsrmTooBig
    llm/
      port.py           # LLMProvider Protocol
      bedrock.py        # BedrockProvider (boto3, único lugar con import boto3)
      fake.py           # FakeLLMProvider para tests y desarrollo local
    events/
      consumer.py       # Loop genérico XREADGROUP + dispatch por type
      envelope.py       # Modelo del envelope de crud-ts
    config.py           # Settings (pydantic-settings)
migrations/
  001_user_preference_embeddings.sql
scripts/
  check_osrm_table_limit.py     # Diagnóstico del límite de /table del OSRM desplegado
tests/
  fixtures/
    crud_ts_schema.sql          # Réplica de esquema SOLO para tests (no es migración)
  integration/
    matching/
      test_candidates_postgis.py  # Query de candidatos contra PostGIS real
  unit/
    matching/                   # detour, scoring, router (200/422/503), events
    preferences/
      test_handler.py           # Idempotencia, reintento, DLQ
      test_schema_extraction.py # 4 casos en español con Stubber
    shared/
      test_bedrock_provider.py  # Stubber sin llamadas reales
      test_osrm_client.py       # respx: lon/lat, nulls, reintento, 503
      test_http_app.py          # /health
```
