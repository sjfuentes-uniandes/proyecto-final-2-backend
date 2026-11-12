-- Coberturas paramétricas, evaluaciones y activaciones de pagos-parametricos (HU-W21, DS3-19 a
-- DS3-22). Idempotente: migrar() lo aplica en cada arranque. El outbox es el de 001_descartes.sql
-- (HU-W24), que HU-W21 comparte: aquí no se recrea.

-- La copia local de la cobertura de cada póliza (se alimenta de los eventos Poliza*, sin llamadas
-- síncronas a polizas). Solo avanza con un orden mayor (DS3-22): `aggregate_version` si el evento y
-- la fila lo traen; si no, `ultimo_evento_en` (el occurredAt del último evento aplicado). Una
-- cancelación que llega antes que la emisión deja la fila CANCELADA sin regla, para recordar el
-- orden: una emisión más vieja ya no la reactiva.
CREATE TABLE IF NOT EXISTS coberturas_activas (
  poliza_id uuid PRIMARY KEY,
  numero text NULL,
  estado text NOT NULL CHECK (estado IN ('ACTIVA', 'CANCELADA')),
  tipo_evento text NULL CHECK (tipo_evento IN ('LLUVIA_ACUMULADA', 'RETRASO_VUELO')),
  zona text NULL CHECK (zona ~ '^[A-Z0-9]{2,16}$'),
  umbral numeric(12, 3) NULL CHECK (umbral > 0),
  unidad text NULL CHECK (unidad IN ('mm', 'min')),
  valor_pago numeric(18, 2) NULL CHECK (valor_pago > 0),
  moneda char(3) NULL CHECK (moneda ~ '^[A-Z]{3}$'),
  version_regla text NULL CHECK (length(version_regla) BETWEEN 1 AND 32),
  -- Ventana semiabierta [vigente_desde, vigente_hasta) (la vigencia de la póliza, en UTC).
  vigente_desde timestamptz NULL,
  vigente_hasta timestamptz NULL,
  aggregate_version int NULL CHECK (aggregate_version >= 1),
  ultimo_evento_en timestamptz NOT NULL,
  actualizado_en timestamptz NOT NULL DEFAULT now(),
  -- Una cobertura ACTIVA siempre trae su regla completa.
  CONSTRAINT cobertura_activa_con_regla CHECK (
    estado = 'CANCELADA'
    OR (tipo_evento IS NOT NULL AND zona IS NOT NULL AND umbral IS NOT NULL
        AND unidad IS NOT NULL AND valor_pago IS NOT NULL AND moneda IS NOT NULL
        AND version_regla IS NOT NULL AND vigente_desde IS NOT NULL
        AND vigente_hasta IS NOT NULL AND vigente_hasta > vigente_desde)
  )
);

-- Las candidatas de un evento (DS3-19): solo las ACTIVA del mismo tipo y zona, sin recorrer el resto.
CREATE INDEX IF NOT EXISTS coberturas_activas_candidatas
  ON coberturas_activas (tipo_evento, zona) WHERE estado = 'ACTIVA';
CREATE INDEX IF NOT EXISTS coberturas_activas_numero ON coberturas_activas (numero)
  WHERE numero IS NOT NULL;

-- Una decisión por evento, la original: si el evento vuelve a llegar (aunque su eventId ya no esté
-- en el inbox), `duplicados` crece y la decisión no cambia (AC4). Guarda el origen (fuente e
-- id_externo) y la razón a nivel de evento; la de cada póliza está en `evaluaciones`.
CREATE TABLE IF NOT EXISTS eventos_evaluados (
  evento_id uuid PRIMARY KEY,
  fuente text NOT NULL CHECK (fuente ~ '^[A-Z][A-Z0-9-]{1,31}$'),
  id_externo text NOT NULL CHECK (id_externo ~ '^[A-Za-z0-9._:-]{1,64}$'),
  evento_origen text NOT NULL CHECK (evento_origen ~ '^[A-Za-z0-9._:-]{1,64}$'),
  decision text NOT NULL CHECK (decision IN ('ACTIVA', 'NO_ACTIVA', 'SIN_POLIZAS')),
  razon text NULL CHECK (razon IN ('SIN_POLIZAS_EN_ZONA')),
  evaluadas int NOT NULL CHECK (evaluadas >= 0),
  activaciones int NOT NULL CHECK (activaciones >= 0 AND activaciones <= evaluadas),
  duplicados int NOT NULL DEFAULT 0 CHECK (duplicados >= 0),
  version_regla text NOT NULL,
  evaluado_en timestamptz NOT NULL,
  CONSTRAINT razon_de_sin_polizas CHECK ((decision = 'SIN_POLIZAS') = (razon IS NOT NULL))
);

-- Una fila por evento y póliza candidata: la clave única es la barrera de negocio contra la doble
-- evaluación. Solo candidatas de la misma zona y tipo; un evento sin candidatas no escribe aquí.
CREATE TABLE IF NOT EXISTS evaluaciones (
  evento_id uuid NOT NULL REFERENCES eventos_evaluados (evento_id),
  poliza_id uuid NOT NULL,
  decision text NOT NULL CHECK (decision IN ('ACTIVA', 'NO_ACTIVA')),
  razon text NULL CHECK (razon IN ('POLIZA_NO_ACTIVA', 'FUERA_DE_VENTANA', 'FUERA_DE_ZONA',
                                   'BAJO_UMBRAL')),
  version_regla text NULL,
  evaluado_en timestamptz NOT NULL,
  UNIQUE (evento_id, poliza_id),
  CONSTRAINT razon_de_no_activa CHECK ((decision = 'NO_ACTIVA') = (razon IS NOT NULL))
);

-- Lo que W22 convierte en un siniestro: una activación por evento y póliza que activó, PENDIENTE
-- hasta que el siniestro exista. Guarda lo que W22 necesita (valor, moneda y versión de la regla)
-- tal como estaba al evaluar. Se crea aquí para no migrar dos veces la misma tabla en la semana.
CREATE TABLE IF NOT EXISTS activaciones (
  evento_id uuid NOT NULL REFERENCES eventos_evaluados (evento_id),
  poliza_id uuid NOT NULL,
  estado text NOT NULL DEFAULT 'PENDIENTE' CHECK (estado ~ '^[A-Z_]{3,32}$'),
  siniestro_id uuid NULL,
  valor_pago numeric(18, 2) NOT NULL CHECK (valor_pago > 0),
  moneda char(3) NOT NULL CHECK (moneda ~ '^[A-Z]{3}$'),
  version_regla text NOT NULL,
  creada_en timestamptz NOT NULL,
  UNIQUE (evento_id, poliza_id)
);
CREATE INDEX IF NOT EXISTS activaciones_pendientes ON activaciones (creada_en)
  WHERE estado = 'PENDIENTE';

-- El DDL recomendado por solventa_common.inbox: deduplica por eventId los mensajes de las dos
-- colas. Sin sobre ni payload.
CREATE TABLE IF NOT EXISTS inbox (
  event_id uuid PRIMARY KEY,
  event_type text NOT NULL,
  recibido_en timestamptz NOT NULL
);
