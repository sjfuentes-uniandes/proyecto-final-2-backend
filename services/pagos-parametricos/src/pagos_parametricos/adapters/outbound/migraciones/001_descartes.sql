-- Cola de descarte de parametricos-eventos (HU-W24). Idempotente: migrar() lo aplica en cada
-- arranque. HU-W21 agrega sus tablas (inbox, coberturas_activas, evaluaciones) en 002 en adelante.

-- La causa del último fallo de cada mensaje, por MessageId (SQS lo conserva al moverlo a la DLQ).
-- La escribe el consumidor de parametricos-eventos con `al_fallar`, fuera de su transacción. Solo
-- el desenlace, el tipo de error o motivo y las recepciones: nunca el cuerpo.
CREATE TABLE IF NOT EXISTS fallos_consumo (
  message_id text PRIMARY KEY,
  event_id uuid NULL,
  event_type text NULL,
  outcome text NOT NULL,
  detalle text NULL,
  recepciones int NULL,
  ultimo_en timestamptz NOT NULL
);

-- Una resolución por mensaje de la DLQ. `original` y `corregido` son los cuerpos (contenido del
-- evento de telemetría, sin datos personales); también viajan a auditoria en el evento.
CREATE TABLE IF NOT EXISTS resoluciones_descarte (
  message_id text PRIMARY KEY,
  evento_id text NULL,
  accion text NOT NULL CHECK (accion IN ('REPROCESADO', 'CORREGIDO', 'DESCARTADO')),
  causa text NOT NULL,
  actor_id text NOT NULL,
  motivo text NULL,
  original text NOT NULL,
  corregido text NULL CHECK ((accion = 'CORREGIDO') = (corregido IS NOT NULL)),
  resuelto_en timestamptz NOT NULL
);

-- Mismo esquema que el outbox de polizas (S1-D2): el relay lo publica. HU-W21 publica aquí también.
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
