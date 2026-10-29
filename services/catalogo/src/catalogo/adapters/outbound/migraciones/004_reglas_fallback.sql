-- Reglas de fallback versionadas (HU-W06). Idempotente: migrar() lo aplica en cada arranque.
-- Inmutables como las demás reglas: un cambio es una versión nueva. A diferencia de las reglas de
-- prima, pueden vencer (vigente_hasta): un permiso para degradar puede ser temporal. Se publican
-- solo por semilla, como las de suscripción (DS2-18).
CREATE TABLE IF NOT EXISTS reglas_fallback (
  producto_id text NOT NULL REFERENCES productos (id),
  version text NOT NULL CHECK (version ~ '^\d{4}\.\d{2}\.\d+$'),
  vigente_desde timestamptz NOT NULL,
  vigente_hasta timestamptz CHECK (vigente_hasta IS NULL OR vigente_hasta > vigente_desde),
  definicion jsonb NOT NULL,
  hash text NOT NULL,
  publicado_en timestamptz NOT NULL DEFAULT now(),
  publicado_por text NOT NULL,
  PRIMARY KEY (producto_id, version)
);

CREATE INDEX IF NOT EXISTS reglas_fallback_vigencia
  ON reglas_fallback (producto_id, vigente_desde DESC);

CREATE OR REPLACE FUNCTION reglas_fallback_inmutables() RETURNS trigger LANGUAGE plpgsql AS
  $$BEGIN RAISE EXCEPTION 'reglas_fallback es inmutable'; END$$;

DROP TRIGGER IF EXISTS reglas_fallback_inmutables ON reglas_fallback;
CREATE TRIGGER reglas_fallback_inmutables BEFORE UPDATE OR DELETE ON reglas_fallback
  FOR EACH ROW EXECUTE FUNCTION reglas_fallback_inmutables();
