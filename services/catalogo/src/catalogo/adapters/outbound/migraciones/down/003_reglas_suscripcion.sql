-- Reversa de 003_reglas_suscripcion.sql (HU-W13). Solo para pruebas de migración y retrocesos
-- planificados: borra las reglas de suscripción publicadas.
DROP TABLE IF EXISTS reglas_suscripcion;
DROP FUNCTION IF EXISTS reglas_suscripcion_inmutables();
