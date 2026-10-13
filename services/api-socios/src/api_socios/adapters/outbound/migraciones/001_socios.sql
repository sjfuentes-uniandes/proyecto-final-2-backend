-- HU-W01: socios de distribución y su credencial. El secreto no se almacena.
CREATE SEQUENCE IF NOT EXISTS socio_secuencia MAXVALUE 999999;

CREATE TABLE IF NOT EXISTS socio (
    id           TEXT PRIMARY KEY CHECK (id ~ '^SOC-[0-9]{6}$'),
    nombre       TEXT NOT NULL,
    canal        TEXT NOT NULL,
    plan         TEXT NOT NULL,
    permisos     TEXT[] NOT NULL CHECK (cardinality(permisos) > 0),
    estado       TEXT NOT NULL CHECK (estado IN ('activo', 'desactivado')),
    client_id    TEXT NOT NULL UNIQUE,
    api_key_id   TEXT NOT NULL UNIQUE,
    creado       TIMESTAMPTZ NOT NULL,
    desactivado  TIMESTAMPTZ
);
