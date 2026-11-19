-- Activaciones convertidas en siniestros y seguimiento de H09 (HU-W22; DS3-15, DS3-23, R-177,
-- R-179). Idempotente: migrar() lo aplica en cada arranque.

-- Lo que el siniestro necesita de la póliza y la cobertura no guardaba: el cliente (titular) y el
-- nombre del ramo, de `PolizaEmitida`. Nulos en las coberturas aplicadas antes de esta migración:
-- su activación no puede crear el siniestro hasta que se vuelva a publicar su `PolizaEmitida`.
ALTER TABLE coberturas_activas ADD COLUMN IF NOT EXISTS cliente_id uuid NULL;
ALTER TABLE coberturas_activas ADD COLUMN IF NOT EXISTS ramo text NULL;

-- El siniestro de la activación (W22): su código y lo que disparó el pago, que H09 muestra en
-- «Últimos pagos activados» y que se escriben junto con `SINIESTRO_CREADO` (la transacción de W21
-- no cambia). `disparador` = {tipoEvento, valorObservado, umbral, unidad}.
ALTER TABLE activaciones ADD COLUMN IF NOT EXISTS siniestro_codigo text NULL;
ALTER TABLE activaciones ADD COLUMN IF NOT EXISTS disparador jsonb NULL;

-- El estado y el siniestro van juntos: `SINIESTRO_CREADO` si y solo si hay `siniestro_id`.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'activacion_siniestro_segun_estado') THEN
    ALTER TABLE activaciones ADD CONSTRAINT activacion_siniestro_segun_estado
      CHECK ((estado = 'SINIESTRO_CREADO') = (siniestro_id IS NOT NULL)
             AND (siniestro_id IS NOT NULL OR (siniestro_codigo IS NULL AND disparador IS NULL)));
  END IF;
END
$$;

-- H09 lee por `evento_origen` y `evaluado_en` (resumen, ritmo de los últimos 15 minutos y
-- selector de orígenes), sin recorrer el resto de la tabla (DS3-19).
CREATE INDEX IF NOT EXISTS eventos_evaluados_por_origen ON eventos_evaluados (evento_origen, evaluado_en DESC);
CREATE INDEX IF NOT EXISTS eventos_evaluados_por_fecha ON eventos_evaluados (evaluado_en DESC);
-- «Últimos pagos activados»: las activaciones con siniestro, la más reciente primero.
CREATE INDEX IF NOT EXISTS activaciones_con_siniestro ON activaciones (creada_en DESC)
  WHERE estado = 'SINIESTRO_CREADO';
