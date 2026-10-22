-- Productos de seguro y reglas de prima versionadas (HU-W03). Idempotente: migrar() lo aplica en
-- cada arranque. El producto puede cambiar de estado; las reglas de prima son inmutables, como los
-- modelos de riesgo: una regla nueva es una versión nueva.
CREATE TABLE IF NOT EXISTS productos (
  id text PRIMARY KEY CHECK (id ~ '^[a-z0-9-]{1,64}$'),
  version text NOT NULL CHECK (version ~ '^\d{4}\.\d{2}\.\d+$'),
  nombre text NOT NULL,
  ramo text NOT NULL,
  pais char(2) NOT NULL CHECK (pais ~ '^[A-Z]{2}$'),
  moneda char(3) NOT NULL CHECK (moneda ~ '^[A-Z]{3}$'),
  estado text NOT NULL CHECK (estado IN ('ACTIVO', 'INACTIVO')),
  vigente_desde timestamptz NOT NULL,
  -- Vigencia semiabierta [vigente_desde, vigente_hasta); NULL = sin fin.
  vigente_hasta timestamptz NULL CHECK (vigente_hasta IS NULL OR vigente_hasta > vigente_desde),
  coberturas jsonb NOT NULL,
  actualizado_en timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reglas_prima (
  producto_id text NOT NULL REFERENCES productos (id),
  version text NOT NULL CHECK (version ~ '^\d{4}\.\d{2}\.\d+$'),
  vigente_desde timestamptz NOT NULL,
  definicion jsonb NOT NULL,
  hash text NOT NULL,
  publicado_en timestamptz NOT NULL DEFAULT now(),
  publicado_por text NOT NULL,
  PRIMARY KEY (producto_id, version)
);

CREATE INDEX IF NOT EXISTS reglas_prima_vigencia ON reglas_prima (producto_id, vigente_desde DESC);

CREATE OR REPLACE FUNCTION reglas_prima_inmutables() RETURNS trigger LANGUAGE plpgsql AS
  $$BEGIN RAISE EXCEPTION 'reglas_prima es inmutable'; END$$;

DROP TRIGGER IF EXISTS reglas_prima_inmutables ON reglas_prima;
CREATE TRIGGER reglas_prima_inmutables BEFORE UPDATE OR DELETE ON reglas_prima
  FOR EACH ROW EXECUTE FUNCTION reglas_prima_inmutables();
