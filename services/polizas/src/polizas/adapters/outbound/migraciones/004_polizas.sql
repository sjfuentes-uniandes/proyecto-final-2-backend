-- Pólizas (HU-W17, 17.6; ADR 0014, decisiones 1 a 3). Idempotente: migrar() lo aplica en cada
-- arranque.
--
-- Una póliza por aceptación (= claveRecaudo), y solo de una aceptación conocida: la crea el primer
-- RecaudoConfirmado, en la transacción del inbox. Nace EMITIENDO con el número ya reservado
-- (<ramo>-<año>-<6 dígitos>, de la secuencia polizas_numero; un número que nunca llega a ACTIVA
-- deja un hueco) y la vigencia en fechas de America/Bogota. Dentro de EMITIENDO, dos puntos de
-- control para no repetir trabajo al reintentar: el documento subido (completo o nada) y la firma
-- de su huella (solo con el documento). ACTIVA exige los dos y emitida_en, y es final. El fallo
-- tiene forma de código, como en dinero: sin texto libre del proveedor ni PII.
CREATE SEQUENCE IF NOT EXISTS polizas_numero;

CREATE TABLE IF NOT EXISTS polizas (
  poliza_id uuid PRIMARY KEY,
  aceptacion_id uuid NOT NULL UNIQUE REFERENCES aceptaciones (id),
  cotizacion_id uuid NOT NULL,
  cliente_id uuid NOT NULL,
  producto_id text NOT NULL CHECK (btrim(producto_id) <> ''),
  producto_version text NOT NULL CHECK (btrim(producto_version) <> ''),
  -- El ramo con el que se numeró (tabla de constantes de polizas, decisión 2).
  ramo_codigo text NOT NULL CHECK (ramo_codigo ~ '^[0-9]{3}$'),
  ramo_nombre text NOT NULL CHECK (btrim(ramo_nombre) <> ''),
  numero text NOT NULL UNIQUE CHECK (numero ~ '^[0-9]{3}-[0-9]{4}-[0-9]{6}$'),
  -- Fechas inclusivas en America/Bogota.
  vigencia_desde date NOT NULL,
  vigencia_hasta date NOT NULL,
  estado text NOT NULL CHECK (estado IN ('EMITIENDO', 'ACTIVA')),
  documento_clave text NULL CHECK (btrim(documento_clave) <> ''),
  documento_version_id text NULL CHECK (btrim(documento_version_id) <> ''),
  documento_huella text NULL CHECK (documento_huella ~ '^sha256:[0-9a-f]{64}$'),
  documento_tamano_bytes bigint NULL CHECK (documento_tamano_bytes > 0),
  -- La fecha que lleva el PDF; al activar es emitida_en.
  documento_fecha timestamptz NULL,
  firma_id text NULL CHECK (btrim(firma_id) <> ''),
  firma_sello_tiempo timestamptz NULL,
  emitida_en timestamptz NULL,
  ultimo_fallo text NULL CHECK (ultimo_fallo ~ '^[A-Z][A-Z0-9_]{0,63}$'),
  creada_en timestamptz NOT NULL,
  actualizada_en timestamptz NOT NULL,
  CONSTRAINT polizas_vigencia CHECK (vigencia_hasta > vigencia_desde),
  CONSTRAINT polizas_numero_del_ramo CHECK (starts_with(numero, ramo_codigo || '-')),
  CONSTRAINT polizas_documento_completo CHECK (
    num_nulls(documento_clave, documento_version_id, documento_huella, documento_tamano_bytes,
      documento_fecha) IN (0, 5)
  ),
  CONSTRAINT polizas_firma_completa CHECK (num_nulls(firma_id, firma_sello_tiempo) IN (0, 2)),
  CONSTRAINT polizas_firma_con_documento CHECK (firma_id IS NULL OR documento_clave IS NOT NULL),
  CONSTRAINT polizas_activa_completa CHECK (
    estado <> 'ACTIVA'
    OR (documento_clave IS NOT NULL AND firma_id IS NOT NULL AND emitida_en IS NOT NULL)
  ),
  CONSTRAINT polizas_emitiendo_sin_emision CHECK (estado <> 'EMITIENDO' OR emitida_en IS NULL)
);

-- Una póliza no se borra, y una ACTIVA no cambia. Mientras está EMITIENDO solo cambian el estado,
-- los puntos de control aún vacíos, emitida_en, el fallo y actualizada_en: la identidad no, y un
-- punto de control ya registrado tampoco (un reintento lo reutiliza).
CREATE OR REPLACE FUNCTION polizas_protegidas() RETURNS trigger LANGUAGE plpgsql AS
  $$BEGIN
    IF TG_OP = 'DELETE' THEN
      RAISE EXCEPTION '% no admite DELETE', TG_TABLE_NAME;
    END IF;
    IF OLD.estado = 'ACTIVA' THEN
      RAISE EXCEPTION '%: una poliza ACTIVA es final', TG_TABLE_NAME;
    END IF;
    IF (NEW.poliza_id, NEW.aceptacion_id, NEW.cotizacion_id, NEW.cliente_id, NEW.producto_id,
        NEW.producto_version, NEW.ramo_codigo, NEW.ramo_nombre, NEW.numero, NEW.vigencia_desde,
        NEW.vigencia_hasta, NEW.creada_en)
       IS DISTINCT FROM
       (OLD.poliza_id, OLD.aceptacion_id, OLD.cotizacion_id, OLD.cliente_id, OLD.producto_id,
        OLD.producto_version, OLD.ramo_codigo, OLD.ramo_nombre, OLD.numero, OLD.vigencia_desde,
        OLD.vigencia_hasta, OLD.creada_en) THEN
      RAISE EXCEPTION '%: la identidad de la poliza no cambia', TG_TABLE_NAME;
    END IF;
    IF OLD.documento_clave IS NOT NULL
       AND (NEW.documento_clave, NEW.documento_version_id, NEW.documento_huella,
            NEW.documento_tamano_bytes, NEW.documento_fecha)
           IS DISTINCT FROM
           (OLD.documento_clave, OLD.documento_version_id, OLD.documento_huella,
            OLD.documento_tamano_bytes, OLD.documento_fecha) THEN
      RAISE EXCEPTION '%: el documento registrado no cambia', TG_TABLE_NAME;
    END IF;
    IF OLD.firma_id IS NOT NULL
       AND (NEW.firma_id, NEW.firma_sello_tiempo)
           IS DISTINCT FROM (OLD.firma_id, OLD.firma_sello_tiempo) THEN
      RAISE EXCEPTION '%: la firma registrada no cambia', TG_TABLE_NAME;
    END IF;
    RETURN NEW;
  END$$;

DROP TRIGGER IF EXISTS polizas_protegidas ON polizas;
CREATE TRIGGER polizas_protegidas BEFORE UPDATE OR DELETE ON polizas
  FOR EACH ROW EXECUTE FUNCTION polizas_protegidas();
