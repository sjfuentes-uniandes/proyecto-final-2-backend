-- Consentimiento (ADR 0009, 0010). Idempotente: migrar() reejecuta todos los .sql en cada arranque.
-- La reversa está en migraciones/down/002_consentimientos.sql (migrar() no recorre esa carpeta).
--
-- Excepción documentada: los tres estados, `revocado_en` y el disparador existen desde la Parte 1
-- aunque revocar llegue en la Parte 2, para no alterar restricciones en migraciones que se
-- reejecutan. `proposito` y `alcances` no llevan CHECK de valores: agregar uno es aditivo y lo
-- valida el dominio.
CREATE TABLE IF NOT EXISTS consentimiento (
    id            UUID PRIMARY KEY,
    cliente_id    UUID NOT NULL,
    proposito     TEXT NOT NULL,
    alcances      TEXT[] NOT NULL CHECK (cardinality(alcances) >= 1),
    version_texto TEXT NOT NULL,
    idioma        TEXT NOT NULL,
    estado        TEXT NOT NULL CHECK (estado IN ('ACTIVO', 'REVOCADO', 'VENCIDO')),
    otorgado_en   TIMESTAMPTZ NOT NULL,
    vigente_hasta TIMESTAMPTZ NOT NULL,
    revocado_en   TIMESTAMPTZ NULL,
    -- Sin CASCADE: borrar un cliente no puede arrastrar la evidencia de sus consentimientos.
    CONSTRAINT consentimiento_cliente_fk FOREIGN KEY (cliente_id) REFERENCES cliente (id),
    CHECK (vigente_hasta > otorgado_en),
    CHECK ((estado = 'REVOCADO') = (revocado_en IS NOT NULL))
);

-- Un solo ACTIVO por (cliente, propósito), sobre el estado almacenado.
CREATE UNIQUE INDEX IF NOT EXISTS consentimiento_activo_unico
    ON consentimiento (cliente_id, proposito) WHERE estado = 'ACTIVO';
CREATE INDEX IF NOT EXISTS consentimiento_cliente
    ON consentimiento (cliente_id, otorgado_en DESC);

-- La clave de idempotencia es por cliente, no global (ADR 0009). Retención de 24 h: la barre
-- el propio otorgamiento.
CREATE TABLE IF NOT EXISTS consentimiento_idempotencia (
    cliente_id        UUID NOT NULL,
    clave             UUID NOT NULL,
    hash_cuerpo       TEXT NOT NULL,
    consentimiento_id UUID NOT NULL REFERENCES consentimiento (id),
    creado_en         TIMESTAMPTZ NOT NULL,
    CONSTRAINT consentimiento_idempotencia_pk PRIMARY KEY (cliente_id, clave)
);
CREATE INDEX IF NOT EXISTS consentimiento_idempotencia_creado
    ON consentimiento_idempotencia (creado_en);

-- Mismas columnas que el outbox de `cotizacion`, para que el relay compartido de
-- `solventa_common` sirva a ambos servicios (P1-8). El relay llega en la PR 2.2: aquí solo se
-- insertan filas, con `published_at` nulo.
CREATE TABLE IF NOT EXISTS outbox (
    event_id       UUID PRIMARY KEY,
    aggregate_id   UUID NOT NULL,
    event_type     TEXT NOT NULL,
    event_version  INT NOT NULL,
    occurred_at    TIMESTAMPTZ NOT NULL,
    correlation_id TEXT NOT NULL,
    payload        JSONB NOT NULL,
    published_at   TIMESTAMPTZ NULL,
    attempts       INT NOT NULL DEFAULT 0,
    last_error     TEXT NULL
);
CREATE INDEX IF NOT EXISTS outbox_pendientes ON outbox (occurred_at) WHERE published_at IS NULL;

-- `vigente_hasta` se calcula en la base con la misma captura de `otorgado_en` (ADR 0010):
-- 6 o 12 meses en UTC; si el día no existe en el mes de destino, el último día del mes. Fija la
-- zona UTC para no depender de la zona de la sesión.
CREATE OR REPLACE FUNCTION fin_de_vigencia(otorgado TIMESTAMPTZ, meses INT)
RETURNS TIMESTAMPTZ LANGUAGE sql IMMUTABLE AS
$$ SELECT ((otorgado AT TIME ZONE 'UTC') + make_interval(months => meses)) AT TIME ZONE 'UTC' $$;

-- Solo ACTIVO -> REVOCADO o ACTIVO -> VENCIDO; solo cambian `estado` y `revocado_en`.
CREATE OR REPLACE FUNCTION consentimiento_inmutable() RETURNS trigger LANGUAGE plpgsql AS
$$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'consentimiento es inmutable: no se elimina';
    END IF;
    IF OLD.estado <> 'ACTIVO' THEN
        RAISE EXCEPTION 'consentimiento % ya es final (%)', OLD.id, OLD.estado;
    END IF;
    IF NEW.estado NOT IN ('REVOCADO', 'VENCIDO') THEN
        RAISE EXCEPTION 'transición de consentimiento no permitida: % -> %', OLD.estado, NEW.estado;
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.cliente_id IS DISTINCT FROM OLD.cliente_id
       OR NEW.proposito IS DISTINCT FROM OLD.proposito
       OR NEW.alcances IS DISTINCT FROM OLD.alcances
       OR NEW.version_texto IS DISTINCT FROM OLD.version_texto
       OR NEW.idioma IS DISTINCT FROM OLD.idioma
       OR NEW.otorgado_en IS DISTINCT FROM OLD.otorgado_en
       OR NEW.vigente_hasta IS DISTINCT FROM OLD.vigente_hasta THEN
        RAISE EXCEPTION 'columnas inmutables de consentimiento %', OLD.id;
    END IF;
    RETURN NEW;
END
$$;

-- Se crea solo si no existe: DROP TRIGGER en cada arranque tomaría un bloqueo en cada despliegue.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'consentimiento_inmutable' AND tgrelid = 'consentimiento'::regclass
    ) THEN
        CREATE TRIGGER consentimiento_inmutable BEFORE UPDATE OR DELETE ON consentimiento
            FOR EACH ROW EXECUTE FUNCTION consentimiento_inmutable();
    END IF;
END
$$;
