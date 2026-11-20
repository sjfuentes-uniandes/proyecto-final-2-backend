-- No se deshacen reportes ya enviados ni sus archivos permanentes.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'borradores' AND column_name = 'estado'
    ) THEN
        LOCK TABLE borradores IN ACCESS EXCLUSIVE MODE;
        IF EXISTS (SELECT 1 FROM borradores WHERE estado = 'CONVERTIDO') THEN
            RAISE EXCEPTION 'No se puede revertir 006: existen borradores convertidos';
        END IF;
    END IF;
END $$;

DROP INDEX IF EXISTS siniestros_por_cliente;
DROP INDEX IF EXISTS borradores_abiertos_por_actor;
ALTER TABLE IF EXISTS evidencias DROP COLUMN IF EXISTS duracion_segundos;
ALTER TABLE IF EXISTS siniestros DROP COLUMN IF EXISTS descripcion;
ALTER TABLE IF EXISTS borradores DROP CONSTRAINT IF EXISTS borrador_conversion_completa;
ALTER TABLE IF EXISTS borradores DROP COLUMN IF EXISTS envio_resultado;
ALTER TABLE IF EXISTS borradores DROP COLUMN IF EXISTS siniestro_id;
ALTER TABLE IF EXISTS borradores DROP COLUMN IF EXISTS estado;
ALTER TABLE IF EXISTS borradores DROP COLUMN IF EXISTS version;
ALTER TABLE IF EXISTS borradores DROP COLUMN IF EXISTS fecha_evento;
ALTER TABLE IF EXISTS borradores DROP COLUMN IF EXISTS causa;
