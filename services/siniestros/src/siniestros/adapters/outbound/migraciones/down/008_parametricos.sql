-- Reversa de 008_parametricos.sql (HU-W22). Solo para pruebas de migración y retrocesos planificados:
-- borra la medición guardada de los paramétricos. Se aplica **antes** que la reversa de 002.
DROP INDEX IF EXISTS siniestros_parametrico_evento_poliza;
ALTER TABLE IF EXISTS siniestros DROP COLUMN IF EXISTS evento;
