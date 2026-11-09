-- Reversa de 002_decisiones.sql (HU-W20). Solo para pruebas de migración y retrocesos planificados:
-- borra las decisiones y la liquidación guardada. Se aplica **antes** que la reversa de 001.
DROP INDEX IF EXISTS siniestros_plazo_soporte;
ALTER TABLE IF EXISTS siniestros DROP CONSTRAINT IF EXISTS siniestros_liquidacion_segun_estado;
ALTER TABLE IF EXISTS siniestros DROP CONSTRAINT IF EXISTS siniestros_plazo_segun_estado;
ALTER TABLE IF EXISTS siniestros DROP COLUMN IF EXISTS valor_indemnizar;
ALTER TABLE IF EXISTS siniestros DROP COLUMN IF EXISTS deducible;
DROP TABLE IF EXISTS decisiones_siniestro;
