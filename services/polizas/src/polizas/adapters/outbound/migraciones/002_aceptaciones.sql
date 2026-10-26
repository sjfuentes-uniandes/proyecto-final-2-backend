-- Aceptaciones de la oferta (HU-W15, W15-B04). Idempotente: migrar() lo aplica en cada arranque.
-- La aceptación es evidencia inmutable: una sola por cotización (DS2-15) y por Idempotency-Key
-- (DS2-11), sobre la decisión de esa cotización. Su evento OfertaAceptada va al outbox de 001 en
-- la misma transacción. `vigente_hasta` (R-88) permite recalcular la huella desde la fila sola.
CREATE TABLE IF NOT EXISTS aceptaciones (
  id uuid PRIMARY KEY,
  cotizacion_id uuid NOT NULL UNIQUE,
  oferta_id uuid NULL,
  decision_id uuid NOT NULL UNIQUE REFERENCES decisiones_suscripcion (id),
  cliente_id uuid NOT NULL,
  idempotency_key uuid NOT NULL UNIQUE,
  huella_solicitud text NOT NULL,
  huella_calculo text NOT NULL,
  vigente_hasta timestamptz NOT NULL,
  version_declaraciones text NOT NULL,
  declaraciones jsonb NOT NULL,
  canal text NOT NULL CHECK (canal IN ('PORTAL_ASESOR')),
  actor_id text NOT NULL,
  aceptada_en timestamptz NOT NULL CHECK (aceptada_en < vigente_hasta),
  huella_evidencia text NOT NULL CHECK (huella_evidencia ~ '^sha256:[0-9a-f]{64}$'),
  correlation_id text NOT NULL
);

CREATE OR REPLACE FUNCTION aceptaciones_inmutables() RETURNS trigger LANGUAGE plpgsql AS
  $$BEGIN RAISE EXCEPTION '% es inmutable', TG_TABLE_NAME; END$$;

DROP TRIGGER IF EXISTS aceptaciones_inmutables ON aceptaciones;
CREATE TRIGGER aceptaciones_inmutables BEFORE UPDATE OR DELETE ON aceptaciones
  FOR EACH ROW EXECUTE FUNCTION aceptaciones_inmutables();
