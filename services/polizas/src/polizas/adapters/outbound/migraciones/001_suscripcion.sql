-- Decisiones de suscripción y outbox de polizas (HU-W13, W13-B17). Idempotente: migrar() lo
-- aplica en cada arranque. La decisión no cambia nunca (regla 13): se guarda con sus entradas, la
-- versión y la huella de las reglas, y se reconstruye con ellas. Una sola decisión por cotización
-- (DS2-05).
CREATE SEQUENCE IF NOT EXISTS decisiones_codigo;

CREATE TABLE IF NOT EXISTS decisiones_suscripcion (
  id uuid PRIMARY KEY,
  codigo text NOT NULL UNIQUE CHECK (codigo ~ '^DEC-\d{4}-\d{6}$'),
  cotizacion_id uuid NOT NULL UNIQUE,
  oferta_id uuid NULL,
  cotizacion_codigo text NULL,
  cliente_id uuid NOT NULL,
  producto_id text NOT NULL,
  idempotency_key uuid NOT NULL UNIQUE,
  huella_solicitud text NOT NULL,
  resultado text NOT NULL CHECK (resultado IN ('APROBADA', 'RECHAZADA', 'REVISION_REQUERIDA')),
  tipo text NOT NULL CHECK (tipo IN ('AUTOMATICA')),
  regla_determinante text NULL,
  reglas_evaluadas jsonb NOT NULL,
  motivos jsonb NOT NULL,
  insumos jsonb NOT NULL,
  hash_insumos text NOT NULL,
  version_reglas text NOT NULL,
  hash_reglas text NOT NULL,
  decidida_en timestamptz NOT NULL,
  duracion_ms integer NOT NULL CHECK (duracion_ms >= 0),
  correlation_id text NOT NULL,
  actor_id text NOT NULL
);
CREATE INDEX IF NOT EXISTS decisiones_resultado
  ON decisiones_suscripcion (resultado, decidida_en, id);
CREATE INDEX IF NOT EXISTS decisiones_producto ON decisiones_suscripcion (producto_id, resultado);
-- Edad, suma, plazo y datos del perfil: sin fecha de nacimiento ni nombre, pero sensibles.
COMMENT ON COLUMN decisiones_suscripcion.insumos IS 'sensible';

CREATE OR REPLACE FUNCTION decisiones_inmutables() RETURNS trigger LANGUAGE plpgsql AS
  $$BEGIN RAISE EXCEPTION '% es inmutable', TG_TABLE_NAME; END$$;

DROP TRIGGER IF EXISTS decisiones_inmutables ON decisiones_suscripcion;
CREATE TRIGGER decisiones_inmutables BEFORE UPDATE OR DELETE ON decisiones_suscripcion
  FOR EACH ROW EXECUTE FUNCTION decisiones_inmutables();

-- Mismo esquema que el outbox de cotizacion (S1-D2): el relay lo publica (EXT-04).
CREATE TABLE IF NOT EXISTS outbox (
  event_id uuid PRIMARY KEY,
  aggregate_id uuid NOT NULL,
  event_type text NOT NULL,
  event_version int NOT NULL,
  occurred_at timestamptz NOT NULL,
  correlation_id text NOT NULL,
  payload jsonb NOT NULL,
  published_at timestamptz NULL,
  attempts int NOT NULL DEFAULT 0,
  last_error text NULL
);
CREATE INDEX IF NOT EXISTS outbox_pendientes ON outbox (occurred_at) WHERE published_at IS NULL;
