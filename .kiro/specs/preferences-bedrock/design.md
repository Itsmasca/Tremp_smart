# Design — slice `preferences` con proveedor Bedrock

## 1. Visión general

```
Redis Streams                matching-py                      Postgres
rides.events  ──XREADGROUP──► consumer.py ──dispatch──► handler.py
                                                              │
                                                    ┌─────────┴──────────┐
                                                    ▼                    ▼
                                             LLMProvider            repository.py
                                          (port.py Protocol)             │
                                          ┌──────────────┐    ┌──────────┴──────────┐
                                          │BedrockProvider│    │  user_preferences   │
                                          │  fake.py      │    │  (structured cols)  │
                                          └──────────────┘    │                     │
                                                              │user_preference_     │
                                                              │  embeddings         │
                                                              └─────────────────────┘
```

El slice es **completamente asíncrono** respecto a `POST /match`. El ranker LightGBM leerá `structured` y el embedding cuando necesite features, pero ese flujo no es responsabilidad de este slice.

---

## 2. Estructura de archivos

```
app/
├── features/
│   └── preferences/
│       ├── __init__.py
│       ├── handler.py        # lógica de negocio del evento
│       ├── schema.py         # StructuredPreferences (Pydantic v2)
│       ├── repository.py     # acceso a DB (UPDATE condicional + upsert embedding)
│       └── prompts.py        # system prompt en español con contexto Mérida
├── shared/
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── port.py           # LLMProvider Protocol
│   │   ├── bedrock.py        # BedrockProvider
│   │   └── fake.py           # FakeLLMProvider
│   ├── events/
│   │   ├── __init__.py
│   │   ├── consumer.py       # loop XREADGROUP genérico
│   │   └── envelope.py       # PreferencesUpdatedEnvelope (Pydantic)
│   └── config.py             # Settings (pydantic-settings)
migrations/
└── 001_user_preference_embeddings.sql
tests/
├── unit/
│   ├── preferences/
│   │   ├── test_handler.py         # idempotencia, DLQ, reintento
│   │   └── test_schema_extraction.py  # casos en español
│   └── shared/
│       └── test_bedrock_provider.py   # Stubber sin llamadas reales
└── conftest.py
```

---

## 3. Modelos de datos

### 3.1 Envelope (entrada, inmutable — dueño: crud-ts)

```python
# app/shared/events/envelope.py
class PreferencesUpdatedData(BaseModel):
    userId: str
    revision: int
    rawText: str

class PreferencesUpdatedEnvelope(BaseModel):
    id: str
    type: Literal["preferences.updated"]
    version: int
    source: str
    aggregateType: str
    aggregateId: str
    occurredAt: datetime
    data: PreferencesUpdatedData
```

### 3.2 StructuredPreferences

```python
# app/features/preferences/schema.py
class TimeWindow(BaseModel):
    days: list[Weekday]
    start: str   # HH:MM
    end: str     # HH:MM

class StructuredPreferences(BaseModel):
    schema_version: Literal[1] = 1
    gender_preference: Literal["any", "same_only"] = "any"
    allow_pets: bool | None = None
    allow_smoking: bool | None = None
    allow_music: bool | None = None
    max_detour_minutes: int | None = None
    preferred_areas: list[str] = []
    avoided_areas: list[str] = []
    time_windows: list[TimeWindow] = []
    avoid_strangers_at_night: bool | None = None
    notes: str | None = None
    confidence: float  # 0..1, validado con ge=0, le=1
```

### 3.3 Tabla `user_preference_embeddings` (propiedad de matching-py)

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE user_preference_embeddings (
    user_id     UUID        PRIMARY KEY,
    revision    INT         NOT NULL,
    embedding   vector(256) NOT NULL,   -- dimensión = EMBED_DIM
    model_id    TEXT        NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

La dimensión se parametriza en la migración mediante una variable SQL o se aplica con el valor de `EMBED_DIM` en el momento de ejecutar la migración.

---

## 4. Puerto `LLMProvider`

```python
# app/shared/llm/port.py
from typing import Protocol
from app.features.preferences.schema import StructuredPreferences

class LLMProvider(Protocol):
    def extract_preferences(self, raw_text: str) -> StructuredPreferences: ...
    def embed(self, text: str) -> list[float]: ...
```

El `Protocol` permite sustituir `BedrockProvider` por `FakeLLMProvider` sin ningún cambio en el handler.

---

## 5. `BedrockProvider` — decisiones de diseño

### 5.1 Extracción con Converse API + tool use forzado

Se define una herramienta `extract_preferences` cuyo `inputSchema` es el JSON Schema derivado de `StructuredPreferences`. La llamada usa `toolChoice = {"tool": {"name": "extract_preferences"}}` para forzar que el modelo rellene el esquema en lugar de responder con texto.

```
converse(
  modelId=LLM_MODEL_ID,
  system=[{"text": SYSTEM_PROMPT}],
  messages=[{"role": "user", "content": [{"text": raw_text}]}],
  toolConfig={
    "tools": [{"toolSpec": {"name": "extract_preferences", "inputSchema": {"json": SCHEMA}}}],
    "toolChoice": {"tool": {"name": "extract_preferences"}}
  }
)
```

La respuesta se extrae de `response["output"]["message"]["content"][0]["toolUse"]["input"]` y se valida con `StructuredPreferences.model_validate(...)`.

### 5.2 Reintentos con backoff

```python
# Pseudocódigo
for attempt in range(MAX_RETRIES):
    try:
        return client.converse(...)
    except ClientError as e:
        if e.response["Error"]["Code"] in {"ThrottlingException", "ModelTimeoutException"}:
            sleep(base * 2**attempt + jitter())
        else:
            raise
```

`MAX_RETRIES=3`, `base=1s`.

### 5.3 Embeddings (Titan Text Embeddings V2)

```python
invoke_model(
    modelId=EMBED_MODEL_ID,
    body=json.dumps({"inputText": text, "dimensions": EMBED_DIM, "normalize": True})
)
# respuesta: body["embedding"]
```

### 5.4 IDs de modelos y perfiles de inferencia

| Variable | Default | Notas |
|----------|---------|-------|
| `BEDROCK_LLM_MODEL_ID` | `us.anthropic.claude-3-5-haiku-20241022-v1:0` | Cross-region inference profile US |
| `BEDROCK_EMBED_MODEL_ID` | `amazon.titan-embed-text-v2:0` | Disponible en us-east-1 |
| `BEDROCK_REGION` | `us-east-1` | |
| `EMBED_DIM` | `256` | Titan V2 soporta 256, 512, 1024 |

El prefijo `us.` en el LLM model ID indica uso del *cross-region inference profile*, que distribuye carga entre regiones US y aumenta el throughput disponible.

---

## 6. Handler — flujo completo

```
handle_preferences_updated(envelope, llm, repo, redis, config)
│
├── 1. Leer structured_revision actual de DB
├── 2. Si event.revision <= structured_revision → ACK, return
│
├── 3. Llamar llm.extract_preferences(raw_text)   [intento 1]
│        └── ValidationError → reintentar con error en prompt  [intento 2]
│                └── ValidationError → XADD rides.events.dlq → ACK, return
│
├── 4. repo.update_structured(user_id, structured, revision)   [UPDATE condicional]
├── 5. embedding = llm.embed(raw_text)
├── 6. repo.upsert_embedding(user_id, revision, embedding, model_id)
└── 7. XACK
```

El UPDATE condicional en el paso 4 usa `WHERE structured_revision IS NULL OR structured_revision < :revision` para ser resistente a condiciones de carrera entre múltiples instancias del servicio.

---

## 7. Consumer genérico

```python
# app/shared/events/consumer.py
async def run_consumer(redis, stream, group, consumer_name, handlers: dict[str, Callable]):
    # Al arrancar: XAUTOCLAIM para recuperar pendientes
    await _autoclaim_pending(redis, stream, group, consumer_name, handlers)
    # Loop principal
    while True:
        messages = await redis.xreadgroup(group, consumer_name, {stream: ">"}, count=10, block=5000)
        for stream_name, entries in messages:
            for msg_id, fields in entries:
                envelope_type = _parse_type(fields["envelope"])
                handler = handlers.get(envelope_type)
                if handler:
                    await handler(fields["envelope"])
                await redis.xack(stream, group, msg_id)
```

El `dict[str, Callable]` permite registrar handlers para múltiples tipos sin acoplar el consumer a ningún slice específico.

---

## 8. Configuración (`pydantic-settings`)

```python
class Settings(BaseSettings):
    # Redis
    REDIS_URL: str
    REDIS_STREAM: str = "rides.events"
    REDIS_DLQ_STREAM: str = "rides.events.dlq"
    REDIS_CONSUMER_GROUP: str = "matching-preferences"
    REDIS_CONSUMER_NAME: str = "matching-py-1"

    # Postgres
    DATABASE_URL: str

    # Bedrock
    BEDROCK_REGION: str = "us-east-1"
    BEDROCK_LLM_MODEL_ID: str = "us.anthropic.claude-3-5-haiku-20241022-v1:0"
    BEDROCK_EMBED_MODEL_ID: str = "amazon.titan-embed-text-v2:0"
    EMBED_DIM: int = 256

    # Proveedor
    LLM_PROVIDER: Literal["bedrock", "fake"] = "bedrock"

    # Misc
    LOG_LEVEL: str = "INFO"
```

---

## 9. Prompt de extracción

El system prompt (en `prompts.py`) está en español y contiene:
1. Rol del asistente (extractor de preferencias para rides en Mérida, Yucatán).
2. Instrucción explícita de no inventar campos no mencionados.
3. Contexto geográfico: colonias y zonas relevantes de Mérida (Altabrisa, Montejo, Norte, Centro, etc.) para ayudar al modelo a normalizar nombres de áreas.
4. Formato de horario esperado (24h, HH:MM).

---

## 10. Tests

### Casos unitarios del handler

| Test | Qué verifica |
|------|-------------|
| `test_idempotency_old_revision` | Revisión vieja → no escribe, hace ACK |
| `test_idempotency_same_revision` | Revisión igual → no escribe, hace ACK |
| `test_happy_path` | Revisión nueva → extrae, escribe, embebe, ACK |
| `test_validation_retry_success` | Primer intento falla validación, segundo OK |
| `test_validation_retry_dlq` | Ambos intentos fallan → DLQ + ACK |

### Casos de extracción en español (FakeLLMProvider customizable / parametrizado)

| Input | Esperado |
|-------|---------|
| `"prefiero ir solo con mujeres"` | `gender_preference="same_only"` |
| `"no fumo y llevo a mi perro"` | `allow_smoking=False, allow_pets=True` |
| `"puedo desviarme 10 min si es por Altabrisa"` | `max_detour_minutes=10, preferred_areas=["Altabrisa"]` |
| `"entre semana salgo 7:30"` | `time_windows=[{days:[mon,tue,wed,thu,fri], start:"07:30", ...}]` |

Estos casos se ejecutan contra `BedrockProvider` con `botocore.stub.Stubber` (respuestas prefabricadas), verificando que el parseo de la respuesta Converse API sea correcto.

---

## 11. Permisos IAM mínimos

```json
{
  "Effect": "Allow",
  "Action": ["bedrock:InvokeModel"],
  "Resource": [
    "arn:aws:bedrock:us-east-1::foundation-model/amazon.titan-embed-text-v2:0",
    "arn:aws:bedrock:*::foundation-model/anthropic.claude-3-5-haiku-20241022-v1:0",
    "arn:aws:bedrock:us-east-1:*:inference-profile/us.anthropic.claude-3-5-haiku-20241022-v1:0"
  ]
}
```

Para el cross-region inference profile (`us.*`) se necesita además permitir el ARN del perfil de inferencia en la cuenta.
