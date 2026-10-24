-- =============================================================================
-- Migración 001: tabla user_preference_embeddings
-- Propietario: matching-py
-- Requiere: extensión pgvector instalada en Postgres
--
-- Ajustar EMBED_DIM antes de ejecutar si no se usa el default (256).
-- Titan Text Embeddings V2 soporta: 256, 512, 1024
-- =============================================================================

-- Extensión pgvector (idempotente)
CREATE EXTENSION IF NOT EXISTS vector;

-- Tabla principal (idempotente)
CREATE TABLE IF NOT EXISTS user_preference_embeddings (
    user_id    UUID        NOT NULL,
    revision   INT         NOT NULL,
    -- Cambiar 256 por el valor de EMBED_DIM si se configura diferente
    embedding  vector(256) NOT NULL,
    model_id   TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT user_preference_embeddings_pk PRIMARY KEY (user_id)
);

COMMENT ON TABLE user_preference_embeddings IS
    'Embeddings de preferencias de usuario generados por matching-py con Bedrock. '
    'Propietario: matching-py. user_id referencia user_preferences(user_id) de crud-ts.';

COMMENT ON COLUMN user_preference_embeddings.revision IS
    'Revisión de preferencias que generó este embedding (sincronizado con user_preferences.structured_revision).';

COMMENT ON COLUMN user_preference_embeddings.model_id IS
    'Model ID de Bedrock usado para generar el embedding (ej: amazon.titan-embed-text-v2:0).';

-- Índice HNSW para búsqueda por similitud coseno (< distancia coseno = más similar)
-- Parámetros conservadores; ajustar m y ef_construction según volumen de datos.
CREATE INDEX IF NOT EXISTS user_preference_embeddings_hnsw_idx
    ON user_preference_embeddings
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
