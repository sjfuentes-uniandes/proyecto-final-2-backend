-- Modelos de riesgo versionados e inmutables (04 §B.5). Idempotente: migrar() lo aplica en cada arranque.
CREATE TABLE IF NOT EXISTS modelos_riesgo (
  version text PRIMARY KEY CHECK (version ~ '^\d{4}\.\d{2}\.\d+$'),
  producto_id text NOT NULL,
  vigente_desde timestamptz NOT NULL,
  definicion jsonb NOT NULL,
  hash text NOT NULL,
  publicado_en timestamptz NOT NULL DEFAULT now(),
  publicado_por text NOT NULL
);

CREATE INDEX IF NOT EXISTS modelos_vigencia ON modelos_riesgo (producto_id, vigente_desde DESC);

CREATE OR REPLACE FUNCTION modelos_inmutables() RETURNS trigger LANGUAGE plpgsql AS
  $$BEGIN RAISE EXCEPTION 'modelos_riesgo es inmutable'; END$$;

DROP TRIGGER IF EXISTS modelos_inmutables ON modelos_riesgo;
CREATE TRIGGER modelos_inmutables BEFORE UPDATE OR DELETE ON modelos_riesgo
  FOR EACH ROW EXECUTE FUNCTION modelos_inmutables();
