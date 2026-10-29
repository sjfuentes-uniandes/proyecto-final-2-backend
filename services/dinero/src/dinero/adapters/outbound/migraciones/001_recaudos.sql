-- Movimientos de recaudo, outbox e inbox de dinero (HU-W16; ADR 0013, decisiones 2, 4, 5 y 7).
-- Idempotente: migrar() lo aplica en cada arranque.
--
-- Un movimiento por claveRecaudo (= aceptacionId de OfertaAceptada): el índice único resuelve las
-- entregas duplicadas o concurrentes, y la clave es también la idempotente hacia el proveedor. Nace
-- PENDIENTE (con commit, antes de cobrar) y solo una respuesta definitiva lo lleva a CONFIRMADO o
-- RECHAZADO, que son finales. Guarda solo lo que trae el evento: ningún dato de tarjeta ni de
-- cuenta; el pagador es el clienteId sintético. Los códigos (motivo, ultimo_incierto) tienen forma
-- de código, como en el dominio: sin texto libre del proveedor ni PII.
CREATE TABLE IF NOT EXISTS recaudos (
  recaudo_id uuid PRIMARY KEY,
  clave_recaudo uuid NOT NULL UNIQUE,
  cotizacion_id uuid NOT NULL,
  pagador_ref uuid NOT NULL,
  -- numeric sin escala: guarda el texto decimal del evento tal cual, sin redondear.
  monto numeric NOT NULL CHECK (monto > 0),
  moneda text NOT NULL CHECK (moneda IN ('COP')),
  -- Informativa: v1 cobra solo el primer recaudo.
  periodicidad text NOT NULL CHECK (periodicidad <> ''),
  estado text NOT NULL CHECK (estado IN ('PENDIENTE', 'CONFIRMADO', 'RECHAZADO')),
  transaccion_id text NULL
    CHECK (btrim(transaccion_id) <> '' AND length(transaccion_id) <= 128),
  motivo text NULL CHECK (motivo ~ '^[A-Z][A-Z0-9_]{0,63}$'),
  ultimo_incierto text NULL CHECK (ultimo_incierto ~ '^[A-Z][A-Z0-9_]{0,63}$'),
  creado_en timestamptz NOT NULL,
  actualizado_en timestamptz NOT NULL,
  resuelto_en timestamptz NULL,
  correlation_id text NOT NULL,
  CONSTRAINT recaudos_confirmado_con_transaccion
    CHECK (estado <> 'CONFIRMADO' OR (transaccion_id IS NOT NULL AND motivo IS NULL)),
  CONSTRAINT recaudos_rechazado_con_motivo CHECK (estado <> 'RECHAZADO' OR motivo IS NOT NULL),
  CONSTRAINT recaudos_pendiente_sin_resultado
    CHECK (estado <> 'PENDIENTE' OR (transaccion_id IS NULL AND motivo IS NULL)),
  CONSTRAINT recaudos_resuelto_si_final CHECK ((estado = 'PENDIENTE') = (resuelto_en IS NULL))
);

-- Un movimiento no se borra, y uno CONFIRMADO o RECHAZADO no cambia. Un PENDIENTE solo cambia su
-- resultado (estado, transacción, motivo, incierto y marcas de tiempo): lo que trae el evento, no.
CREATE OR REPLACE FUNCTION recaudos_protegidos() RETURNS trigger LANGUAGE plpgsql AS
  $$BEGIN
    IF TG_OP = 'DELETE' THEN
      RAISE EXCEPTION '% no admite DELETE', TG_TABLE_NAME;
    END IF;
    IF OLD.estado <> 'PENDIENTE' THEN
      RAISE EXCEPTION '%: un movimiento % es final', TG_TABLE_NAME, OLD.estado;
    END IF;
    IF (NEW.recaudo_id, NEW.clave_recaudo, NEW.cotizacion_id, NEW.pagador_ref, NEW.monto,
        NEW.moneda, NEW.periodicidad, NEW.creado_en, NEW.correlation_id)
       IS DISTINCT FROM
       (OLD.recaudo_id, OLD.clave_recaudo, OLD.cotizacion_id, OLD.pagador_ref, OLD.monto,
        OLD.moneda, OLD.periodicidad, OLD.creado_en, OLD.correlation_id) THEN
      RAISE EXCEPTION '%: lo que trae el evento no cambia', TG_TABLE_NAME;
    END IF;
    RETURN NEW;
  END$$;

DROP TRIGGER IF EXISTS recaudos_protegidos ON recaudos;
CREATE TRIGGER recaudos_protegidos BEFORE UPDATE OR DELETE ON recaudos
  FOR EACH ROW EXECUTE FUNCTION recaudos_protegidos();

-- Mismo esquema que el outbox de polizas (y de cotizacion, S1-D2): lo publica el relay de
-- solventa_common (16.4). published_at solo lo marca el relay.
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

-- El DDL recomendado por solventa_common.inbox (16.5): deduplica OfertaAceptada por eventId. Sin
-- sobre ni payload.
CREATE TABLE IF NOT EXISTS inbox (
  event_id uuid PRIMARY KEY,
  event_type text NOT NULL,
  recibido_en timestamptz NOT NULL
);
