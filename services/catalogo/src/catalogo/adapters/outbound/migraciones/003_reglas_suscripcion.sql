-- Reglas de suscripción versionadas (HU-W13, DS2-02). Idempotente: migrar() lo aplica en cada
-- arranque. Inmutables como las reglas de prima: un cambio de reglas es una versión nueva. Se
-- publican solo por semilla en el Sprint 2 (DS2-18).
CREATE TABLE IF NOT EXISTS reglas_suscripcion (
  producto_id text NOT NULL REFERENCES productos (id),
  version text NOT NULL CHECK (version ~ '^\d{4}\.\d{2}\.\d+$'),
  vigente_desde timestamptz NOT NULL,
  definicion jsonb NOT NULL,
  hash text NOT NULL,
  publicado_en timestamptz NOT NULL DEFAULT now(),
  publicado_por text NOT NULL,
  PRIMARY KEY (producto_id, version)
);

CREATE INDEX IF NOT EXISTS reglas_suscripcion_vigencia
  ON reglas_suscripcion (producto_id, vigente_desde DESC);

CREATE OR REPLACE FUNCTION reglas_suscripcion_inmutables() RETURNS trigger LANGUAGE plpgsql AS
  $$BEGIN RAISE EXCEPTION 'reglas_suscripcion es inmutable'; END$$;

DROP TRIGGER IF EXISTS reglas_suscripcion_inmutables ON reglas_suscripcion;
CREATE TRIGGER reglas_suscripcion_inmutables BEFORE UPDATE OR DELETE ON reglas_suscripcion
  FOR EACH ROW EXECUTE FUNCTION reglas_suscripcion_inmutables();
