-- Decisiones de fallback (HU-W06). Aditiva e idempotente: migrar() la aplica en cada arranque.
-- Una fila por recorrido que encontró una fuente del perfil sin obtener (AC3): la fuente, el dato,
-- el motivo, la regla y su versión, y la decisión. Solo identificadores y motivos: ningún dato
-- personal ni financiero. DEGRADADA queda unida a su cotización (en la misma transacción);
-- PENDIENTE no tiene cotización y es única por actor y clave de idempotencia. Inmutable.
CREATE TABLE IF NOT EXISTS decisiones_fallback (
  id uuid PRIMARY KEY,
  cotizacion_id uuid NULL UNIQUE REFERENCES cotizaciones (id),
  perfil_id uuid NOT NULL,
  producto_id text NOT NULL,
  tipo text NOT NULL CHECK (tipo IN ('DEGRADADA', 'PENDIENTE')),
  faltantes jsonb NOT NULL,
  mensaje_codigo text NOT NULL,
  version_reglas text NULL,
  hash_reglas text NULL,
  ajustes jsonb NOT NULL,
  actor_id text NOT NULL,
  idempotency_key uuid NOT NULL,
  huella_solicitud text NOT NULL,
  correlation_id text NOT NULL,
  decidido_en timestamptz NOT NULL,
  CHECK ((tipo = 'DEGRADADA') = (cotizacion_id IS NOT NULL)),
  CHECK (tipo = 'PENDIENTE' OR version_reglas IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS decisiones_fallback_pendiente_idempotencia
  ON decisiones_fallback (actor_id, idempotency_key) WHERE cotizacion_id IS NULL;

-- perfiles_inmutables() (001) es genérica: lanza con el nombre de la tabla.
DROP TRIGGER IF EXISTS decisiones_fallback_inmutables ON decisiones_fallback;
CREATE TRIGGER decisiones_fallback_inmutables BEFORE UPDATE OR DELETE ON decisiones_fallback
  FOR EACH ROW EXECUTE FUNCTION perfiles_inmutables();
