# Tasks — slice `preferences` con proveedor Bedrock

## T-01 · Actualizar dependencias del proyecto
- [ ] Añadir a `pyproject.toml`: `boto3`, `pydantic>=2`, `pydantic-settings`, `redis>=5`, `asyncpg`, `psycopg[binary]`, `pgvector`
- [ ] Añadir dependencias de dev/test: `pytest`, `pytest-asyncio`, `botocore` (para Stubber)
- [ ] Crear/actualizar `.env.example` con todas las variables de `Settings`

## T-02 · Configuración central (`app/shared/config.py`)
- [ ] Implementar clase `Settings` con `pydantic-settings` según §8 del design
- [ ] Validar que el proceso falle explícitamente si `REDIS_URL` o `DATABASE_URL` están ausentes
- [ ] Exportar instancia `settings = Settings()`

## T-03 · Envelope del evento (`app/shared/events/envelope.py`)
- [ ] Implementar `PreferencesUpdatedData` y `PreferencesUpdatedEnvelope` (Pydantic v2)
- [ ] Validar campo `type: Literal["preferences.updated"]`
- [ ] Función helper `parse_envelope(raw_json: str) -> PreferencesUpdatedEnvelope | None` que retorna `None` para tipos desconocidos

## T-04 · Esquema `StructuredPreferences` (`app/features/preferences/schema.py`)
- [ ] Implementar `Weekday` enum: `mon|tue|wed|thu|fri|sat|sun`
- [ ] Implementar `TimeWindow` con validadores de formato HH:MM
- [ ] Implementar `StructuredPreferences` con todos los campos de RF-03
- [ ] Añadir validador `confidence` con `ge=0, le=1`
- [ ] Exponer `StructuredPreferences.model_json_schema()` como constante `PREFERENCES_JSON_SCHEMA`

## T-05 · Puerto `LLMProvider` (`app/shared/llm/port.py`)
- [ ] Definir `Protocol` `LLMProvider` con `extract_preferences` y `embed`
- [ ] Añadir docstrings con contrato (no levanta en throttling — el proveedor maneja reintentos)

## T-06 · Prompt de extracción (`app/features/preferences/prompts.py`)
- [ ] Escribir `SYSTEM_PROMPT` en español con contexto de Mérida, Yucatán
- [ ] Incluir instrucción explícita: campos no mencionados → `null`, no inventar
- [ ] Listar colonias/zonas de referencia: Altabrisa, Montejo, Norte, García Ginerés, Centro, Itzimná, Chuburná, Campestre, Lomas de Mérida, Cholul, etc.
- [ ] Especificar formato de horario 24h (HH:MM)
- [ ] Función `build_retry_prompt(raw_text: str, validation_error: str) -> str`

## T-07 · `FakeLLMProvider` (`app/shared/llm/fake.py`)
- [ ] Implementar `FakeLLMProvider` que retorne un `StructuredPreferences` fijo y configurable
- [ ] Constructor acepta `response_override: StructuredPreferences | None = None`
- [ ] `embed()` retorna un vector de ceros de longitud `EMBED_DIM`
- [ ] Implementar variante `FailOnceFakeLLMProvider` que lanza `ValidationError` en el primer llamado y retorna respuesta válida en el segundo (para tests de reintento)

## T-08 · `BedrockProvider` (`app/shared/llm/bedrock.py`)
- [ ] Crear cliente `bedrock-runtime` con `botocore.config.Config(connect_timeout=5, read_timeout=30, retries={"mode": "standard"})`
- [ ] Implementar `extract_preferences` con Converse API + tool use forzado (§5.1 del design)
- [ ] Extraer `toolUse.input` de la respuesta y validar con `StructuredPreferences.model_validate`
- [ ] Implementar reintentos manuales con backoff ante `ThrottlingException` y `ModelTimeoutException` (§5.2)
- [ ] Implementar `embed` con `invoke_model` + Titan Text Embeddings V2 (§5.3)
- [ ] Leer todos los parámetros desde `settings`
- [ ] NO importar `boto3` en ningún otro módulo

## T-09 · Factory del proveedor (`app/shared/llm/__init__.py`)
- [ ] Función `get_llm_provider(settings) -> LLMProvider` que retorna `BedrockProvider` o `FakeLLMProvider` según `settings.LLM_PROVIDER`

## T-10 · Repository (`app/features/preferences/repository.py`)
- [ ] Función `get_structured_revision(conn, user_id) -> int | None`
- [ ] Función `update_structured(conn, user_id, structured, revision) -> bool` con UPDATE condicional (`WHERE structured_revision IS NULL OR structured_revision < :revision`); retorna `True` si actualizó
- [ ] Función `upsert_embedding(conn, user_id, revision, embedding, model_id)` con INSERT ON CONFLICT DO UPDATE
- [ ] Usar `asyncpg` o `psycopg` (consistente con el resto del proyecto)

## T-11 · Handler del evento (`app/features/preferences/handler.py`)
- [ ] Función `handle_preferences_updated(envelope, llm, repo, redis_client, settings) -> None`
- [ ] Implementar flujo completo según §6 del design
- [ ] Paso 2: verificar idempotencia por revisión
- [ ] Paso 3: extracción con reintento y DLQ (RF-09)
- [ ] Pasos 4-6: escritura condicional + embedding
- [ ] El handler NO importa `boto3` ni sabe qué proveedor se usa

## T-12 · Consumer genérico (`app/shared/events/consumer.py`)
- [ ] Implementar `run_consumer(redis, stream, group, consumer_name, handlers)` según §7 del design
- [ ] `_autoclaim_pending` al arrancar (XAUTOCLAIM con `min-idle-time=60000ms`)
- [ ] Loop principal `XREADGROUP` con `block=5000ms`, `count=10`
- [ ] ACK solo si el handler no lanza; errores de handler logueados, NO propagan para no romper el loop
- [ ] Crear consumer group con `XGROUP CREATE ... MKSTREAM` si no existe

## T-13 · Migración pgvector (`migrations/001_user_preference_embeddings.sql`)
- [ ] `CREATE EXTENSION IF NOT EXISTS vector`
- [ ] `CREATE TABLE user_preference_embeddings` según §3.3 del design
- [ ] Añadir índice `ivfflat` o `hnsw` en la columna `embedding` para búsqueda por similitud futura
- [ ] Script es idempotente (`IF NOT EXISTS`)

## T-14 · Entry point (`main.py`)
- [ ] Actualizar `main.py` para arrancar el consumer con el handler de preferencias inyectado
- [ ] Inicializar `settings`, `llm_provider`, conexión Postgres, cliente Redis
- [ ] Registrar `{"preferences.updated": handle_preferences_updated}` en el dispatcher

## T-15 · Tests unitarios del handler (`tests/unit/preferences/test_handler.py`)
- [ ] Implementar los 5 casos de la tabla §10 del design
- [ ] Usar `FakeLLMProvider` y `FailOnceFakeLLMProvider`
- [ ] Mockear repo y redis con stubs simples (no requiere DB real)
- [ ] Verificar que DLQ recibe XADD con campo `reason`

## T-16 · Tests del `BedrockProvider` (`tests/unit/shared/test_bedrock_provider.py`)
- [ ] Usar `botocore.stub.Stubber` para simular respuestas Converse API
- [ ] Test `test_extract_preferences_stubbed`: respuesta válida → `StructuredPreferences` correcta
- [ ] Test `test_embed_stubbed`: respuesta Titan → `list[float]` de longitud `EMBED_DIM`
- [ ] Test `test_throttling_retry`: simular `ThrottlingException` en los dos primeros intentos, éxito en el tercero
- [ ] Test `test_throttling_exhausted`: 3 `ThrottlingException` → excepción propagada

## T-17 · Tests de extracción en español (`tests/unit/preferences/test_schema_extraction.py`)
- [ ] Los 4 casos de §10 con `BedrockProvider` + Stubber (respuestas prefabricadas que simulan tool use)
- [ ] Verificar que el parseo de `toolUse.input` produce el `StructuredPreferences` esperado

## T-18 · `__init__.py` de paquetes
- [ ] Crear `__init__.py` vacíos en todos los paquetes nuevos

## T-19 · Actualizar README
- [ ] Sección "Variables de entorno" con tabla de todas las variables de `Settings`
- [ ] Sección "Permisos IAM mínimos" con el JSON de policy de §11 del design
- [ ] Instrucciones para ejecutar local con `LLM_PROVIDER=fake`
- [ ] Instrucciones para aplicar la migración

---

## Orden de implementación sugerido

```
T-01 → T-02 → T-03 → T-04 → T-05 → T-06
T-07 → T-08 → T-09
T-10 → T-11 → T-12
T-13 → T-14
T-15 → T-16 → T-17
T-18 → T-19
```

Los bloques son independientes entre sí después de T-04 y T-05 (schema y port), que son el contrato compartido.
