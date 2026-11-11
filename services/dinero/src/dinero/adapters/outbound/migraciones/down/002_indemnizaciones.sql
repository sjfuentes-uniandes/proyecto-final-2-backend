-- Reversa de 002_indemnizaciones.sql (HU-W23). Solo para pruebas de migración y retrocesos
-- planificados: borra las indemnizaciones (el disparador y el índice caen con su tabla). No toca
-- `recaudos`, `outbox` ni `inbox`. Se aplica antes que la reversa de 001.
DROP TABLE IF EXISTS indemnizaciones;
DROP FUNCTION IF EXISTS indemnizaciones_protegidas();
