-- Auditoría por lotes (HU-W24, D3). Idempotente: migrar() lo aplica en cada arranque.
-- El inbox es el DDL de solventa_common.inbox: deduplica por eventId lo que llega de la cola
-- auditoria, sin sobre ni payload (el evento queda solo en el objeto de S3).
CREATE TABLE IF NOT EXISTS inbox (
  event_id uuid PRIMARY KEY,
  event_type text NOT NULL,
  recibido_en timestamptz NOT NULL
);

-- La cabeza de la cadena: una sola fila. Cada lote la bloquea (FOR UPDATE), escribe su objeto y la
-- avanza en la misma transacción, así que las tareas de auditoria encadenan en serie.
CREATE TABLE IF NOT EXISTS cadena (
  id smallint PRIMARY KEY CHECK (id = 1),
  secuencia bigint NOT NULL CHECK (secuencia >= 0),
  hash text NOT NULL CHECK (hash ~ '^[0-9a-f]{64}$'),
  actualizada_en timestamptz NOT NULL DEFAULT now()
);

INSERT INTO cadena (id, secuencia, hash)
VALUES (1, 0, '0000000000000000000000000000000000000000000000000000000000000000')
ON CONFLICT (id) DO NOTHING;
