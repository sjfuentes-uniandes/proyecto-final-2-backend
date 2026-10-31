-- Dispositivos y entregas de avisos push (HU-M11). Idempotente: migrar() lo aplica en cada arranque.
-- El inbox es el DDL de solventa_common.inbox: deduplica por eventId los mensajes de la cola avisos,
-- sin sobre ni payload.
CREATE TABLE IF NOT EXISTS inbox (
  event_id uuid PRIMARY KEY,
  event_type text NOT NULL,
  recibido_en timestamptz NOT NULL
);

-- Sin PII: solo ids. El token se guarda para poder enviar (nunca va en logs, métricas ni respuestas);
-- su identidad es el sha256, que garantiza un solo dispositivo por token.
CREATE TABLE IF NOT EXISTS dispositivo (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  cliente_id uuid NOT NULL,
  token_hash text NOT NULL UNIQUE CHECK (token_hash ~ '^[0-9a-f]{64}$'),
  token text NOT NULL,
  estado text NOT NULL DEFAULT 'ACTIVO' CHECK (estado IN ('ACTIVO', 'INVALIDO')),
  invalidado_en timestamptz,
  motivo_invalidez text CHECK (motivo_invalidez ~ '^[A-Z0-9_]{1,40}$'),
  registrado_en timestamptz NOT NULL,
  actualizado_en timestamptz NOT NULL,
  CONSTRAINT dispositivo_invalidez CHECK ((estado = 'INVALIDO') = (invalidado_en IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS dispositivo_cliente_idx ON dispositivo (cliente_id) WHERE estado = 'ACTIVO';

-- Una entrega por (evento, dispositivo): la clave única es la deduplicación. `intentos` cuenta los
-- intentos reclamados (también los que murieron con el proceso). `ultimo_fallo` es un código, no
-- texto libre. La entrega guarda lo necesario para armar el push (tipo, poliza_id) y que el barrido
-- no dependa del evento.
CREATE TABLE IF NOT EXISTS entrega (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id uuid NOT NULL,
  dispositivo_id uuid NOT NULL REFERENCES dispositivo (id) ON DELETE CASCADE,
  tipo text NOT NULL,
  poliza_id uuid NOT NULL,
  estado text NOT NULL DEFAULT 'PENDIENTE'
    CHECK (estado IN ('PENDIENTE', 'EN_ENVIO', 'ENVIADO', 'TOKEN_INVALIDO', 'FALLIDO')),
  intentos integer NOT NULL DEFAULT 0 CHECK (intentos >= 0),
  proximo_intento timestamptz NOT NULL,
  reclamada_hasta timestamptz,
  ultimo_fallo text CHECK (ultimo_fallo ~ '^[A-Z0-9_]{1,40}$'),
  creada_en timestamptz NOT NULL,
  actualizada_en timestamptz NOT NULL,
  CONSTRAINT entrega_evento_dispositivo UNIQUE (event_id, dispositivo_id),
  CONSTRAINT entrega_lease CHECK ((estado = 'EN_ENVIO') = (reclamada_hasta IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS entrega_pendiente_idx ON entrega (proximo_intento) WHERE estado = 'PENDIENTE';
CREATE INDEX IF NOT EXISTS entrega_en_envio_idx ON entrega (reclamada_hasta) WHERE estado = 'EN_ENVIO';
