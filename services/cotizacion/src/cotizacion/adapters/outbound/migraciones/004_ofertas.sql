-- Ofertas (HU-W04). Aditiva e idempotente: migrar() la aplica en cada arranque.
-- Una oferta por cotización (entidad del agregado Cotización, sin cambios en los diagramas): se crea
-- en la misma transacción que la cotización y, como ella, no cambia nunca. El estado VIGENTE/VENCIDA
-- no se guarda: se deriva al leer (vigente si ahora < vigente_hasta). `codigo` es el identificador
-- legible que se muestra al cliente (COT-AAAA-NNNNN); las cotizaciones anteriores no tienen oferta.
CREATE SEQUENCE IF NOT EXISTS ofertas_codigo_seq;
CREATE TABLE IF NOT EXISTS ofertas (
  id uuid PRIMARY KEY,
  codigo text NOT NULL UNIQUE CHECK (codigo ~ '^COT-\d{4}-\d{5,}$'),
  cotizacion_id uuid NOT NULL UNIQUE REFERENCES cotizaciones (id),
  precio numeric(20, 4) NOT NULL CHECK (precio >= 0),
  moneda char(3) NOT NULL CHECK (moneda ~ '^[A-Z]{3}$'),
  periodicidad text NOT NULL,
  coberturas jsonb NOT NULL,
  exclusiones jsonb NOT NULL,
  version integer NOT NULL CHECK (version >= 1),
  version_reglas text NOT NULL,
  creada_en timestamptz NOT NULL,
  vigente_hasta timestamptz NOT NULL CHECK (vigente_hasta >= creada_en)
);

-- perfiles_inmutables() (001) es genérica: lanza con el nombre de la tabla.
DROP TRIGGER IF EXISTS ofertas_inmutables ON ofertas;
CREATE TRIGGER ofertas_inmutables BEFORE UPDATE OR DELETE ON ofertas
  FOR EACH ROW EXECUTE FUNCTION perfiles_inmutables();
