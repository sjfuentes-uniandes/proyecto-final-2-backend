-- Siniestros, evidencias, historial, outbox, inbox, idempotencia y secuencia de códigos de
-- siniestros (HU-W19; DS3-02, DS3-03, DS3-11 y DS3-14). Idempotente: migrar() lo aplica en cada
-- arranque. Esquema que usan también W20, W22, W23 y M01 a M05: sus columnas ya están aquí (nulas
-- hasta que cada historia las llene) para no migrar el mismo agregado cuatro veces.
--
-- Sin PII: el cliente es solo un id (su nombre lo compone bff-web desde clientes) y la cuenta del
-- beneficiario viaja enmascarada dentro de `copia_poliza`. `clave_objeto` nunca sale del servicio.

CREATE TABLE IF NOT EXISTS siniestros (
  id uuid PRIMARY KEY,
  -- 88-AAAA-NNNN (DS3-11). El rango 0001-0999 de cada año queda para la semilla de QA.
  codigo text NOT NULL UNIQUE CHECK (codigo ~ '^88-[0-9]{4}-[0-9]{4,}$'),
  origen text NOT NULL CHECK (origen IN ('MOVIL', 'PORTAL', 'PARAMETRICO')),
  estado text NOT NULL
    CHECK (estado IN ('REPORTADO', 'EN_EVALUACION', 'APROBADO', 'RECHAZADO', 'FALTA_SOPORTE')),
  poliza_id uuid NOT NULL,
  cliente_id uuid NOT NULL,
  -- CopiaPolizaDto: la póliza tal como estaba al reportar (fuente de la verificación de cobertura).
  copia_poliza jsonb NOT NULL,
  -- Código del amparo reclamado (uno de copia_poliza.amparos).
  causa text NOT NULL CHECK (btrim(causa) <> ''),
  fecha_evento timestamptz NOT NULL,
  -- {descripcion, lat, lon}
  ubicacion_declarada jsonb NOT NULL,
  -- Inicio del reloj del compromiso de 48 h (DS3-04).
  reportado_en timestamptz NOT NULL,
  analista_id uuid NULL,
  asignado_en timestamptz NULL,
  pausado_desde timestamptz NULL,
  pausa_acumulada_s int NOT NULL DEFAULT 0 CHECK (pausa_acumulada_s >= 0),
  plazo_soporte_vence_en timestamptz NULL,
  -- Detiene el reloj y alimenta los «resueltos de la semana» (R-06).
  decidido_en timestamptz NULL,
  -- Los llena M01; W20 exige valorIndemnizar <= valorReclamado (R-06).
  valor_reclamado numeric(18, 2) NULL CHECK (valor_reclamado >= 0),
  moneda char(3) NULL CHECK (moneda ~ '^[A-Z]{3}$'),
  -- Paramétricos (W22).
  evento_id uuid NULL,
  regla jsonb NULL,
  -- Pago de la indemnización (W23); pago_estado nulo = SIN_PAGO.
  pago_estado text NULL CHECK (pago_estado IN ('PENDIENTE', 'CONFIRMADO', 'RECHAZADO')),
  pago_referencia text NULL,
  pago_transaccion_id text NULL,
  pago_motivo text NULL,
  pago_intentos int NOT NULL DEFAULT 0 CHECK (pago_intentos >= 0),
  pago_duplicados_descartados int NOT NULL DEFAULT 0 CHECK (pago_duplicados_descartados >= 0),
  version int NOT NULL DEFAULT 1 CHECK (version >= 1),
  correlation_id text NOT NULL,
  creado_en timestamptz NOT NULL DEFAULT now(),
  -- Un caso en evaluación o con falta de soporte tiene analista; uno REPORTADO no. Uno decidido
  -- puede no tenerlo (lo aprueba un paramétrico, W22).
  CONSTRAINT siniestros_analista_segun_estado CHECK (
    CASE
      WHEN estado IN ('EN_EVALUACION', 'FALTA_SOPORTE') THEN analista_id IS NOT NULL
      WHEN estado = 'REPORTADO' THEN analista_id IS NULL
      ELSE true
    END
  ),
  CONSTRAINT siniestros_asignado_con_analista CHECK ((analista_id IS NULL) = (asignado_en IS NULL)),
  -- El reloj solo se pausa en FALTA_SOPORTE (W20).
  CONSTRAINT siniestros_pausa_segun_estado CHECK ((estado = 'FALTA_SOPORTE') = (pausado_desde IS NOT NULL)),
  CONSTRAINT siniestros_decidido_segun_estado
    CHECK ((estado IN ('APROBADO', 'RECHAZADO')) = (decidido_en IS NOT NULL))
);

-- Bandeja del analista (H01), cola general (H02) y resumen.
CREATE INDEX IF NOT EXISTS siniestros_por_estado_analista ON siniestros (estado, analista_id, reportado_en);
CREATE INDEX IF NOT EXISTS siniestros_por_analista ON siniestros (analista_id, estado);
CREATE INDEX IF NOT EXISTS siniestros_por_poliza ON siniestros (poliza_id);
-- «Tomar el siguiente» (DS3-03): el REPORTADO más antiguo, con FOR UPDATE SKIP LOCKED.
CREATE INDEX IF NOT EXISTS siniestros_cola ON siniestros (reportado_en, id) WHERE estado = 'REPORTADO';

-- Los archivos del caso. El contenido vive en S3 (siniestros/<siniestroId>/<evidenciaId>); aquí solo
-- sus metadatos y el sha256 con el que se verifica la integridad (DS3-06).
CREATE TABLE IF NOT EXISTS evidencias (
  id uuid PRIMARY KEY,
  siniestro_id uuid NOT NULL REFERENCES siniestros (id),
  nombre_archivo text NOT NULL CHECK (btrim(nombre_archivo) <> '' AND length(nombre_archivo) <= 255),
  tipo text NOT NULL CHECK (tipo IN ('FOTOGRAFIA', 'VIDEO', 'SOPORTE')),
  -- Clasifica un SOPORTE: qué soporte requerido cubre (R-06).
  soporte_tipo text NULL CHECK (
    soporte_tipo IN ('DOCUMENTO_IDENTIDAD', 'FACTURA_COMPRA', 'CERTIFICADO_REPARACION',
                     'COTIZACION_REPUESTOS', 'DENUNCIA_AUTORIDAD', 'PERITAJE')
  ),
  -- Nunca sale del servicio ni se registra.
  clave_objeto text NOT NULL UNIQUE CHECK (btrim(clave_objeto) <> ''),
  sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  capturada_en timestamptz NULL,
  -- {descripcion, lat, lon}
  ubicacion jsonb NULL,
  tamano_bytes bigint NULL CHECK (tamano_bytes >= 0),
  tipo_contenido text NULL,
  CONSTRAINT evidencias_soporte_con_tipo CHECK ((tipo = 'SOPORTE') = (soporte_tipo IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS evidencias_por_siniestro ON evidencias (siniestro_id, capturada_en);

-- Historial de solo inserción (DS3-14): quién hizo qué y cuándo. Los pasos del «Estado del trámite»
-- (H03) se derivan de aquí. Sin PII: el nombre del analista es el de un usuario interno.
CREATE TABLE IF NOT EXISTS historial_siniestro (
  id uuid PRIMARY KEY,
  siniestro_id uuid NOT NULL REFERENCES siniestros (id),
  -- Código en mayúsculas: W20 y W22 agregan los suyos.
  accion text NOT NULL CHECK (accion ~ '^[A-Z_]{3,40}$'),
  estado_anterior text NULL
    CHECK (estado_anterior IN ('REPORTADO', 'EN_EVALUACION', 'APROBADO', 'RECHAZADO', 'FALTA_SOPORTE')),
  estado_nuevo text NULL
    CHECK (estado_nuevo IN ('REPORTADO', 'EN_EVALUACION', 'APROBADO', 'RECHAZADO', 'FALTA_SOPORTE')),
  actor_id text NULL,
  actor_rol text NULL,
  actor_nombre text NULL,
  justificacion text NULL,
  evidencias_consideradas jsonb NULL,
  -- Parámetros de la acción, sin PII.
  detalles jsonb NULL,
  ocurrido_en timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS historial_por_siniestro ON historial_siniestro (siniestro_id, ocurrido_en, id);

CREATE OR REPLACE FUNCTION historial_siniestro_solo_insercion() RETURNS trigger LANGUAGE plpgsql AS
  $$BEGIN
    RAISE EXCEPTION '% es de solo inserción (%)', TG_TABLE_NAME, TG_OP;
  END$$;

DROP TRIGGER IF EXISTS historial_siniestro_solo_insercion ON historial_siniestro;
CREATE TRIGGER historial_siniestro_solo_insercion BEFORE UPDATE OR DELETE ON historial_siniestro
  FOR EACH ROW EXECUTE FUNCTION historial_siniestro_solo_insercion();

-- Mismo esquema que el outbox de dinero (y de polizas): lo publica el relay de solventa_common.
-- published_at solo lo marca el relay.
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

-- El DDL recomendado por solventa_common.inbox: deduplica por eventId (W20/W23 consumen aquí).
CREATE TABLE IF NOT EXISTS inbox (
  event_id uuid PRIMARY KEY,
  event_type text NOT NULL,
  recibido_en timestamptz NOT NULL
);

-- Idempotencia de «tomar el siguiente» (Idempotency-Key): la misma clave devuelve la misma
-- respuesta; la misma clave con otra solicitud es un conflicto (huella distinta).
CREATE TABLE IF NOT EXISTS idempotencia (
  clave text PRIMARY KEY,
  huella_solicitud text NOT NULL,
  respuesta jsonb NOT NULL,
  creada_en timestamptz NOT NULL
);

-- Consecutivo del código 88-AAAA-NNNN por año (DS3-11, R-07). El primero que se entrega es 1000:
-- 0001-0999 queda reservado a la semilla de QA. El upsert corre en una transacción corta.
CREATE TABLE IF NOT EXISTS secuencia_codigo (
  anio int PRIMARY KEY CHECK (anio BETWEEN 2000 AND 9999),
  ultimo int NOT NULL CHECK (ultimo >= 0)
);
