# matching-py

Servicio Python del sistema de rides comunitarios (Mérida, Yucatán).  
Responsabilidades: candidatos H3/PostGIS, desvíos OSRM, ranking LightGBM, extracción de preferencias con Bedrock.

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

---

## Ejecución local sin AWS

```bash
# Instalar dependencias
uv sync

# Configurar sin Bedrock
LLM_PROVIDER=fake REDIS_URL=redis://localhost:6379 DATABASE_URL=postgresql://... python main.py
```

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
```

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
  shared/
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
tests/
  unit/
    preferences/
      test_handler.py           # Idempotencia, reintento, DLQ
      test_schema_extraction.py # 4 casos en español con Stubber
    shared/
      test_bedrock_provider.py  # Stubber sin llamadas reales
```
