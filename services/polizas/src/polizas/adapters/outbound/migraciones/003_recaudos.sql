-- Inbox y proyección del recaudo (HU-W16, 16.13; ADR 0013, decisión 9). Idempotente: migrar() lo
-- aplica en cada arranque.
-- El inbox es el DDL de solventa_common.inbox: deduplica por eventId los mensajes de
-- polizas-recaudos, sin sobre ni payload.
CREATE TABLE IF NOT EXISTS inbox (
  event_id uuid PRIMARY KEY,
  event_type text NOT NULL,
  recibido_en timestamptz NOT NULL
);

-- El primer estado final de cada recaudo, por claveRecaudo (= id de la aceptación), en la misma
-- transacción que el inbox. Sin llave hacia aceptaciones: un evento de una clave que polizas no
-- conoce (la QA de la 16.12) se guarda igual, en lugar de ir a la DLQ. `monto` es numeric sin
-- escala, para conservar el texto decimal del evento. RecaudoRechazado v1 no trae monto: la lectura
-- toma el de la instantánea de la decisión.
CREATE TABLE IF NOT EXISTS recaudos_aceptacion (
  clave_recaudo uuid PRIMARY KEY,
  recaudo_id uuid NOT NULL,
  estado text NOT NULL CHECK (estado IN ('CONFIRMADO', 'RECHAZADO')),
  monto numeric NULL,
  moneda text NULL CHECK (moneda ~ '^[A-Z]{3}$'),
  transaccion_id text NULL,
  motivo text NULL,
  ocurrido_en timestamptz NOT NULL,
  CONSTRAINT recaudos_aceptacion_forma CHECK (
    (estado = 'CONFIRMADO' AND transaccion_id IS NOT NULL AND monto IS NOT NULL
      AND moneda IS NOT NULL AND motivo IS NULL)
    OR (estado = 'RECHAZADO' AND motivo IS NOT NULL AND transaccion_id IS NULL
      AND monto IS NULL AND moneda IS NULL)
  )
);
