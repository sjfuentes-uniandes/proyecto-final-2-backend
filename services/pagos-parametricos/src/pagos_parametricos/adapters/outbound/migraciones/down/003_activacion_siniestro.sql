-- Reversa de 003_activacion_siniestro.sql (HU-W22). Solo para pruebas de migración y retrocesos
-- planificados: borra el cliente y el ramo de las coberturas y el código y el disparador de las
-- activaciones. Se aplica **antes** que la reversa de 002.
DROP INDEX IF EXISTS activaciones_con_siniestro;
DROP INDEX IF EXISTS eventos_evaluados_por_fecha;
DROP INDEX IF EXISTS eventos_evaluados_por_origen;
ALTER TABLE IF EXISTS activaciones DROP CONSTRAINT IF EXISTS activacion_siniestro_segun_estado;
ALTER TABLE IF EXISTS activaciones DROP COLUMN IF EXISTS disparador;
ALTER TABLE IF EXISTS activaciones DROP COLUMN IF EXISTS siniestro_codigo;
ALTER TABLE IF EXISTS coberturas_activas DROP COLUMN IF EXISTS ramo;
ALTER TABLE IF EXISTS coberturas_activas DROP COLUMN IF EXISTS cliente_id;
