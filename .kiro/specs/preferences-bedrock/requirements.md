# Requirements — slice `preferences` con proveedor Bedrock

## 1. Contexto y restricciones del sistema

| ID | Restricción |
|----|-------------|
| C-01 | `matching-py` es el **único** servicio que invoca Bedrock. `crud-ts` no lo toca. |
| C-02 | Bedrock **nunca** queda en el camino síncrono de `POST /match`. Solo se llama al consumir eventos de Redis Streams. |
| C-03 | La tabla `user_preferences` es propiedad de `crud-ts`. Este servicio solo escribe las columnas `structured`, `structured_revision`, `structured_at`. |
| C-04 | La tabla `user_preference_embeddings` es propiedad exclusiva de `matching-py`. |
| C-05 | El contrato del mensaje en `rides.events` está definido por `crud-ts` y no puede modificarse. |

---

## 2. Funcionales

### RF-01 — Consumir evento `preferences.updated`
El servicio DEBE suscribirse al stream `rides.events` usando Redis Streams consumer groups (`XREADGROUP`, grupo `matching-preferences`). Solo debe procesar mensajes cuyo campo `envelope.type` sea `"preferences.updated"`. Cualquier otro `type` debe recibir ACK inmediato sin procesamiento.

### RF-02 — Extraer preferencias estructuradas
Al recibir un evento válido, el servicio DEBE invocar al `LLMProvider` con el `rawText` del evento para obtener un objeto `StructuredPreferences` validado contra el esquema Pydantic v2. La extracción DEBE usar la **Converse API** de Bedrock con *tool use* forzado (`tool_choice = {"type": "tool", "name": "..."}`) para garantizar salida estructurada. Queda prohibido parsear texto libre del LLM.

### RF-03 — Esquema `StructuredPreferences`
El esquema DEBE incluir exactamente los siguientes campos con los tipos indicados:

| Campo | Tipo | Comportamiento |
|-------|------|----------------|
| `schema_version` | `Literal[1]` | Siempre 1 |
| `gender_preference` | `"any" \| "same_only"` | Default `"any"` |
| `allow_pets` | `bool \| None` | `None` si no se menciona |
| `allow_smoking` | `bool \| None` | `None` si no se menciona |
| `allow_music` | `bool \| None` | `None` si no se menciona |
| `max_detour_minutes` | `int \| None` | `None` si no se menciona |
| `preferred_areas` | `list[str]` | Vacío si no se menciona |
| `avoided_areas` | `list[str]` | Vacío si no se menciona |
| `time_windows` | `list[TimeWindow]` | Vacío si no se menciona |
| `avoid_strangers_at_night` | `bool \| None` | `None` si no se menciona |
| `notes` | `str \| None` | Texto residual; `None` si no aplica |
| `confidence` | `float` (0..1) | Certeza del LLM sobre la extracción |

`TimeWindow`: `{days: list[Weekday], start: "HH:MM", end: "HH:MM"}`. `Weekday` = `mon|tue|wed|thu|fri|sat|sun`.

El LLM NO DEBE inventar valores para campos no mencionados.

### RF-04 — Idempotencia y orden
Antes de escribir, el servicio DEBE consultar `structured_revision` actual de la fila. Solo debe proceder si `event.data.revision > structured_revision`. Revisiones iguales o menores deben recibir ACK sin escribir.

### RF-05 — Persistencia de preferencias estructuradas
Tras extracción exitosa, el servicio DEBE escribir en `user_preferences`: `structured = <json>`, `structured_revision = revision`, `structured_at = now()` usando un UPDATE condicional (`WHERE revision > structured_revision`).

### RF-06 — Embeddings
El servicio DEBE generar un embedding del `raw_text` usando `LLMProvider.embed()` y guardarlo en `user_preference_embeddings` con upsert. La dimensión del vector está definida por `EMBED_DIM` (default: 256). El modelo usado DEBE registrarse en la columna `model_id`.

### RF-07 — ACK tras escritura
El ACK al stream (`XACK`) DEBE realizarse solo después de que la escritura en Postgres sea exitosa. Si falla la escritura, el mensaje NO recibe ACK para permitir reprocesamiento.

### RF-08 — Recuperación de pendientes al arrancar
Al iniciar el servicio, DEBE reclamar mensajes pendientes del consumer group con `XAUTOCLAIM` antes de entrar al loop normal de `XREADGROUP`.

### RF-09 — Manejo de errores de validación del LLM
Si la respuesta del LLM no supera la validación Pydantic, el servicio DEBE hacer un único reintento incluyendo el error de validación en el prompt. Si el segundo intento también falla, el mensaje original DEBE enviarse al stream `rides.events.dlq` (XADD) con el motivo del error, y entonces hacer ACK para evitar bucles infinitos.

### RF-10 — Puerto `LLMProvider`
El servicio DEBE definir un `Protocol` `LLMProvider` con exactamente dos métodos:
- `extract_preferences(raw_text: str) -> StructuredPreferences`
- `embed(text: str) -> list[float]`

El handler DEBE recibir el proveedor por inyección de dependencias y NO DEBE importar `boto3` directamente.

### RF-11 — `BedrockProvider`
La implementación DEBE:
- Usar `boto3` cliente `bedrock-runtime` con timeouts configurados (`connect_timeout`, `read_timeout`).
- Implementar reintentos con backoff exponencial ante `ThrottlingException` y `ModelTimeoutException`.
- Leer modelo LLM de `BEDROCK_LLM_MODEL_ID` (default: `us.anthropic.claude-3-5-haiku-20241022-v1:0`).
- Leer modelo de embeddings de `BEDROCK_EMBED_MODEL_ID` (default: `amazon.titan-embed-text-v2:0`).
- Leer región de `BEDROCK_REGION` (default: `us-east-1`).

### RF-12 — `FakeLLMProvider`
DEBE existir un proveedor falso determinista (`LLM_PROVIDER=fake`) para tests locales sin credenciales AWS. Su salida DEBE ser válida contra `StructuredPreferences`.

### RF-13 — Migración pgvector
DEBE existir una migración SQL que cree la tabla `user_preference_embeddings` con la extensión `vector` de pgvector y la dimensión parametrizable.

---

## 3. No funcionales

| ID | Requisito |
|----|-----------|
| NF-01 | Timeouts del cliente boto3: `connect_timeout=5s`, `read_timeout=30s`. |
| NF-02 | Reintentos: máximo 3 intentos, backoff exponencial con jitter (1s, 2s, 4s base). |
| NF-03 | El loop del consumer DEBE ser resistente a fallos transitorios (Redis, Postgres) sin terminar el proceso. |
| NF-04 | Variables de entorno gestionadas con `pydantic-settings`; el proceso DEBE fallar al arrancar si falta una variable requerida. |
| NF-05 | Logs estructurados (JSON) con nivel configurable (`LOG_LEVEL`). |

---

## 4. Fuera de alcance

- Modificar `POST /match` o el ranker LightGBM.
- Crear endpoints HTTP nuevos en `matching-py`.
- Lógica de creación de la tabla `user_preferences` (es de `crud-ts`).
- Autenticación o autorización de mensajes en el stream.
