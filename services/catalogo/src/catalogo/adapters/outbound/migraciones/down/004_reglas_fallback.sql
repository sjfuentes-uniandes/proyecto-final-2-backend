-- Reversa de 004_reglas_fallback.sql (HU-W06). Solo para pruebas de migración y retrocesos
-- planificados: borra las reglas de fallback publicadas (cotizacion vuelve a fallar cerrado).
DROP TABLE IF EXISTS reglas_fallback;
DROP FUNCTION IF EXISTS reglas_fallback_inmutables();
