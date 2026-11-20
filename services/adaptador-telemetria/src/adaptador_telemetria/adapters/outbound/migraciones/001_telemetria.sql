-- Registro de eventos externos recibidos y Outbox de adaptador-telemetria (HU-W21, DS3-16).
-- Idempotente: migrar() lo aplica en cada arranque.

-- Un evento por (fuente, idExterno). `evento_id` es el UUIDv5 de ese par y también el eventId del
-- sobre publicado: el reenvío cae en el ON CONFLICT y no escribe otra fila de Outbox. Solo guarda
-- identificadores y la clave del original (que está en S3): nunca el cuerpo, la firma ni el
-- secreto, y tampoco los valores medidos (van en el Outbox y en el original).
CREATE TABLE IF NOT EXISTS eventos_recibidos (
  evento_id uuid PRIMARY KEY,
  fuente text NOT NULL CHECK (fuente ~ '^[A-Z][A-Z0-9-]{1,31}$'),
  id_externo text NOT NULL CHECK (id_externo ~ '^[A-Za-z0-9._:-]{1,64}$'),
  evento_origen text NOT NULL CHECK (evento_origen ~ '^[A-Za-z0-9._:-]{1,64}$'),
  archivo_ref text NOT NULL,
  correlation_id text NOT NULL,
  recibido_en timestamptz NOT NULL,
  UNIQUE (fuente, id_externo)
);

-- Mismo esquema que el outbox de polizas (S1-D2): el relay lo publica.
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

-- El DDL recomendado por solventa_common.inbox. Este servicio no consume eventos: queda vacía, por
-- uniformidad con los demás servicios de mensajería.
CREATE TABLE IF NOT EXISTS inbox (
  event_id uuid PRIMARY KEY,
  event_type text NOT NULL,
  recibido_en timestamptz NOT NULL
);
