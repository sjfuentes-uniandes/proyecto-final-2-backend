-- Cotizaciones inmutables (HU-W03). Aditiva e idempotente: migrar() la aplica en cada arranque.
-- Se conserva lo que determina la prima (producto, versión y hash de las reglas, entradas mínimas y
-- desglose) y su huella (AC3). Las entradas no guardan la fecha de nacimiento ni el documento: solo
-- la edad cumplida en la fecha de la cotización. Un cambio de estado posterior (oferta, aceptación)
-- se registra aparte: la fila no cambia nunca.
CREATE TABLE IF NOT EXISTS cotizaciones (
  id uuid PRIMARY KEY,
  cliente_id uuid NOT NULL,
  socio_id text NULL CHECK (socio_id ~ '^SOC-\d{6}$'),
  producto_id text NOT NULL,
  version_producto text NOT NULL,
  moneda char(3) NOT NULL CHECK (moneda ~ '^[A-Z]{3}$'),
  periodicidad text NOT NULL,
  version_reglas text NOT NULL,
  hash_reglas text NOT NULL,
  perfil_id uuid NULL REFERENCES perfiles_riesgo (id),
  nivel_riesgo text NULL CHECK (nivel_riesgo IN ('BAJO', 'MEDIO', 'ALTO')),
  entradas jsonb NOT NULL,
  coberturas jsonb NOT NULL,
  desglose jsonb NOT NULL,
  prima_total numeric(20, 4) NOT NULL CHECK (prima_total >= 0),
  huella_calculo text NOT NULL,
  estado text NOT NULL CHECK (estado IN ('CALCULADA')),
  idempotency_key uuid NOT NULL,
  huella_solicitud text NOT NULL,
  calculado_en timestamptz NOT NULL,
  duracion_ms integer NOT NULL,
  correlation_id text NOT NULL,
  actor_id text NOT NULL,
  actor_rol text NOT NULL CHECK (actor_rol IN ('ASESOR', 'SOCIO')),
  UNIQUE (actor_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS cotizaciones_socio ON cotizaciones (socio_id, calculado_en DESC)
  WHERE socio_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS cotizaciones_cliente ON cotizaciones (cliente_id, calculado_en DESC);
COMMENT ON COLUMN cotizaciones.entradas IS 'sensible';

-- perfiles_inmutables() (001) es genérica: lanza con el nombre de la tabla.
DROP TRIGGER IF EXISTS cotizaciones_inmutables ON cotizaciones;
CREATE TRIGGER cotizaciones_inmutables BEFORE UPDATE OR DELETE ON cotizaciones
  FOR EACH ROW EXECUTE FUNCTION perfiles_inmutables();
