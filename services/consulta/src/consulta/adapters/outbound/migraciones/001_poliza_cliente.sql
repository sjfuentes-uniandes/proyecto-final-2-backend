-- Proyección de pólizas del cliente (HU-M10). Idempotente: migrar() lo aplica en cada arranque.
-- El inbox es el DDL de solventa_common.inbox: deduplica por eventId los mensajes de
-- consulta-proyeccion, sin sobre ni payload.
CREATE TABLE IF NOT EXISTS inbox (
  event_id uuid PRIMARY KEY,
  event_type text NOT NULL,
  recibido_en timestamptz NOT NULL
);

-- Una fila por póliza ACTIVA (final e inmutable): se inserta una vez por poliza_id y no se
-- modifica. Ramo, nombre y coberturas son la copia del catálogo al proyectar. Sin PII: solo ids,
-- montos, fechas y referencias al documento y a la firma.
CREATE TABLE IF NOT EXISTS poliza_cliente (
  poliza_id uuid PRIMARY KEY,
  cliente_id uuid NOT NULL,
  numero text NOT NULL UNIQUE,
  estado text NOT NULL CHECK (estado = 'ACTIVA'),
  producto_id text NOT NULL,
  producto_version text NOT NULL,
  producto_nombre text NOT NULL,
  ramo text NOT NULL,
  vigencia_desde date NOT NULL,
  vigencia_hasta date NOT NULL,
  prima numeric NOT NULL,
  suma_asegurada numeric NOT NULL,
  moneda text NOT NULL CHECK (moneda ~ '^[A-Z]{3}$'),
  periodicidad text NOT NULL,
  coberturas jsonb NOT NULL,
  documento_huella text NOT NULL,
  firma_id text NOT NULL,
  emitida_en timestamptz NOT NULL,
  proyectada_en timestamptz NOT NULL,
  CONSTRAINT poliza_cliente_vigencia CHECK (vigencia_desde <= vigencia_hasta)
);

CREATE INDEX IF NOT EXISTS poliza_cliente_cliente_idx ON poliza_cliente (cliente_id);
