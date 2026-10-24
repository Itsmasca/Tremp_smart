-- SOLO TESTS: el esquema real es de crud-ts. No es una migración.
-- Réplica mínima de rides / matches / user_preferences para probar la query
-- de candidatos contra PostGIS real. Si crud-ts cambia su esquema, actualizar
-- esta réplica; la fuente de verdad sigue siendo crud-ts.

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS rides (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    driver_id        UUID NOT NULL,
    origin           GEOMETRY(Point, 4326) NOT NULL,
    destination      GEOMETRY(Point, 4326) NOT NULL,
    route            GEOMETRY(LineString, 4326) NOT NULL,
    duration_s       INTEGER NOT NULL,
    distance_m       INTEGER NOT NULL,
    departure_at     TIMESTAMPTZ NOT NULL,
    seats_available  SMALLINT NOT NULL DEFAULT 1,
    max_detour_s     INTEGER NOT NULL DEFAULT 300,
    status           TEXT NOT NULL DEFAULT 'scheduled'
);

CREATE INDEX IF NOT EXISTS rides_route_gix ON rides USING GIST (route);
CREATE INDEX IF NOT EXISTS rides_departure ON rides (departure_at);

CREATE TABLE IF NOT EXISTS matches (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ride_id      UUID NOT NULL,
    passenger_id UUID NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS user_preferences (
    user_id             UUID PRIMARY KEY,
    raw_text            TEXT,
    revision            INTEGER NOT NULL DEFAULT 0,
    structured          JSONB,
    structured_revision INTEGER,
    structured_at       TIMESTAMPTZ
);
