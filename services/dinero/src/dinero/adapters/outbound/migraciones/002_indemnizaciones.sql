-- Indemnizaciones de dinero (HU-W23; ADR 0015, decisiones 2, 3 y 5; DS3-17 y DS3-18).
-- Idempotente: migrar() lo aplica en cada arranque, después de 001.
--
-- Una indemnización por siniestro y tipo: UNIQUE (siniestro_id, tipo) resuelve los eventos
-- SiniestroAprobado repetidos o concurrentes (otro eventId del mismo siniestro, o dos consumidores a
-- la vez), y `referencia` (IND-<codigo>-01) es única y es también la clave idempotente hacia la
-- pasarela. Nace PENDIENTE (con commit, antes de pagar) y solo una respuesta definitiva la lleva a
-- CONFIRMADA o RECHAZADA, que son finales. Un timeout o un 5xx nunca es un rechazo: sigue PENDIENTE.
-- Guarda solo lo que trae el evento: `beneficiario_ref` es una referencia opaca, nunca la cuenta.
-- Los códigos (motivo, ultimo_incierto) tienen forma de código: sin texto libre de la pasarela.
-- No toca `recaudos` (DS3-17): el patrón se reutiliza, no la tabla.
CREATE TABLE IF NOT EXISTS indemnizaciones (
  indemnizacion_id uuid PRIMARY KEY,
  siniestro_id uuid NOT NULL,
  tipo text NOT NULL CHECK (tipo = 'INDEMNIZACION'),
  referencia text NOT NULL UNIQUE CHECK (referencia ~ '^IND-88-[0-9]{4}-[0-9]{4,}-[0-9]{2}$'),
  monto numeric(20, 4) NOT NULL CHECK (monto > 0),
  moneda text NOT NULL CHECK (moneda IN ('COP')),
  beneficiario_ref text NOT NULL CHECK (btrim(beneficiario_ref) <> '' AND length(beneficiario_ref) <= 128),
  estado text NOT NULL CHECK (estado IN ('PENDIENTE', 'CONFIRMADA', 'RECHAZADA')),
  transaccion_id text NULL
    CHECK (btrim(transaccion_id) <> '' AND length(transaccion_id) <= 128),
  motivo text NULL CHECK (motivo ~ '^[A-Z][A-Z0-9_]{0,63}$'),
  -- Llamadas efectivas de pago a la pasarela, y repeticiones descartadas sin otro movimiento.
  intentos int NOT NULL DEFAULT 0 CHECK (intentos >= 0),
  duplicados_descartados int NOT NULL DEFAULT 0 CHECK (duplicados_descartados >= 0),
  ultimo_incierto text NULL CHECK (ultimo_incierto ~ '^[A-Z][A-Z0-9_]{0,63}$'),
  -- DS3-18: la conciliación reclama un lote hasta este instante; mientras no venza, ninguna otra
  -- réplica toma el movimiento.
  reclamado_hasta timestamptz NULL,
  creado_en timestamptz NOT NULL,
  actualizado_en timestamptz NOT NULL,
  resuelto_en timestamptz NULL,
  correlation_id text NOT NULL,
  CONSTRAINT indemnizaciones_siniestro_tipo UNIQUE (siniestro_id, tipo),
  CONSTRAINT indemnizaciones_confirmada_con_transaccion
    CHECK (estado <> 'CONFIRMADA' OR (transaccion_id IS NOT NULL AND motivo IS NULL)),
  CONSTRAINT indemnizaciones_rechazada_con_motivo
    CHECK (estado <> 'RECHAZADA' OR motivo IS NOT NULL),
  CONSTRAINT indemnizaciones_pendiente_sin_resultado
    CHECK (estado <> 'PENDIENTE' OR (transaccion_id IS NULL AND motivo IS NULL)),
  CONSTRAINT indemnizaciones_resuelta_si_final CHECK ((estado = 'PENDIENTE') = (resuelto_en IS NULL))
);

-- La conciliación lee los PENDIENTE más antiguos: el índice parcial queda pequeño.
CREATE INDEX IF NOT EXISTS indemnizaciones_pendientes
  ON indemnizaciones (actualizado_en, indemnizacion_id) WHERE estado = 'PENDIENTE';

-- Una indemnización no se borra. Lo que trae el evento no cambia nunca. Una PENDIENTE cambia su
-- resultado (estado, transacción, motivo, incierto, intentos, reclamo y marcas de tiempo). Una
-- CONFIRMADA o RECHAZADA es final: su resultado no cambia; solo puede crecer el contador de
-- duplicados descartados (un evento repetido después de pagar suma uno y nada más).
CREATE OR REPLACE FUNCTION indemnizaciones_protegidas() RETURNS trigger LANGUAGE plpgsql AS
  $$BEGIN
    IF TG_OP = 'DELETE' THEN
      RAISE EXCEPTION '% no admite DELETE', TG_TABLE_NAME;
    END IF;
    IF (NEW.indemnizacion_id, NEW.siniestro_id, NEW.tipo, NEW.referencia, NEW.monto, NEW.moneda,
        NEW.beneficiario_ref, NEW.creado_en, NEW.correlation_id)
       IS DISTINCT FROM
       (OLD.indemnizacion_id, OLD.siniestro_id, OLD.tipo, OLD.referencia, OLD.monto, OLD.moneda,
        OLD.beneficiario_ref, OLD.creado_en, OLD.correlation_id) THEN
      RAISE EXCEPTION '%: lo que trae el evento no cambia', TG_TABLE_NAME;
    END IF;
    IF NEW.duplicados_descartados < OLD.duplicados_descartados THEN
      RAISE EXCEPTION '%: los duplicados descartados no disminuyen', TG_TABLE_NAME;
    END IF;
    IF OLD.estado <> 'PENDIENTE' THEN
      IF (NEW.estado, NEW.transaccion_id, NEW.motivo, NEW.intentos, NEW.ultimo_incierto,
          NEW.reclamado_hasta, NEW.actualizado_en, NEW.resuelto_en)
         IS DISTINCT FROM
         (OLD.estado, OLD.transaccion_id, OLD.motivo, OLD.intentos, OLD.ultimo_incierto,
          OLD.reclamado_hasta, OLD.actualizado_en, OLD.resuelto_en) THEN
        RAISE EXCEPTION '%: una indemnización % es final', TG_TABLE_NAME, OLD.estado;
      END IF;
    END IF;
    RETURN NEW;
  END$$;

DROP TRIGGER IF EXISTS indemnizaciones_protegidas ON indemnizaciones;
CREATE TRIGGER indemnizaciones_protegidas BEFORE UPDATE OR DELETE ON indemnizaciones
  FOR EACH ROW EXECUTE FUNCTION indemnizaciones_protegidas();
