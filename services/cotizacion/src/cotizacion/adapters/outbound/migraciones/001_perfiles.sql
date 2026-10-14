-- Perfiles de riesgo inmutables, su linaje y el outbox (04 §B.5, 01 §3.7).
-- Idempotente: migrar() lo aplica en cada arranque.
CREATE TABLE IF NOT EXISTS perfiles_riesgo (
  id uuid PRIMARY KEY,
  cliente_id uuid NOT NULL,
  producto_id text NOT NULL,
  idempotency_key uuid NOT NULL,
  huella_solicitud text NOT NULL,
  datos_declarados jsonb NOT NULL,
  insumos jsonb NOT NULL,
  estado text NOT NULL CHECK (estado IN ('CALCULADO', 'DEGRADADO', 'INFORMACION_INSUFICIENTE')),
  nivel_riesgo text NULL CHECK (nivel_riesgo IN ('BAJO', 'MEDIO', 'ALTO')),
  puntaje numeric(5,2) NULL,
  confianza numeric(3,2) NOT NULL,
  factores jsonb NOT NULL,
  ajustes jsonb NOT NULL,
  datos_requeridos jsonb NOT NULL,
  accion_sugerida text NULL,
  consentimiento jsonb NULL,
  version_modelo text NOT NULL,
  hash_modelo text NOT NULL,
  perfil_anterior_id uuid NULL REFERENCES perfiles_riesgo (id),
  calculado_en timestamptz NOT NULL,
  duracion_ms integer NOT NULL,
  correlation_id text NOT NULL,
  actor_id text NOT NULL,
  UNIQUE (cliente_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS perfiles_cliente ON perfiles_riesgo (cliente_id, calculado_en DESC);
COMMENT ON COLUMN perfiles_riesgo.datos_declarados IS 'sensible';
COMMENT ON COLUMN perfiles_riesgo.insumos IS 'sensible';

CREATE TABLE IF NOT EXISTS linaje_fuentes (
  perfil_id uuid NOT NULL REFERENCES perfiles_riesgo (id),
  orden smallint NOT NULL,
  fuente text NOT NULL,
  tipo text NOT NULL,
  estado text NOT NULL,
  proveedor text NULL,
  version_fuente text NULL,
  confianza numeric(3,2) NULL,
  consultada_en timestamptz NULL,
  duracion_ms integer NULL,
  campos_solicitados text[] NOT NULL,
  campos_disponibles text[] NOT NULL,
  PRIMARY KEY (perfil_id, fuente)
);

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

-- El perfil y su linaje no cambian nunca (regla 13); el outbox sí (lo actualiza el relay).
CREATE OR REPLACE FUNCTION perfiles_inmutables() RETURNS trigger LANGUAGE plpgsql AS
  $$BEGIN RAISE EXCEPTION '% es inmutable', TG_TABLE_NAME; END$$;

DROP TRIGGER IF EXISTS perfiles_inmutables ON perfiles_riesgo;
CREATE TRIGGER perfiles_inmutables BEFORE UPDATE OR DELETE ON perfiles_riesgo
  FOR EACH ROW EXECUTE FUNCTION perfiles_inmutables();

DROP TRIGGER IF EXISTS linaje_inmutable ON linaje_fuentes;
CREATE TRIGGER linaje_inmutable BEFORE UPDATE OR DELETE ON linaje_fuentes
  FOR EACH ROW EXECUTE FUNCTION perfiles_inmutables();
