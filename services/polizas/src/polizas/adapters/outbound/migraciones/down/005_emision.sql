-- Reversa de 005_emision.sql (HU-W17, 17.8). Solo para pruebas de migración y retrocesos
-- planificados: borra el índice de las pendientes, la reserva y la correlación de cada póliza. Se
-- aplica antes que la de 004.
DROP INDEX IF EXISTS polizas_emitiendo;
ALTER TABLE IF EXISTS polizas DROP CONSTRAINT IF EXISTS polizas_correlation_id;
ALTER TABLE IF EXISTS polizas DROP COLUMN IF EXISTS correlation_id;
ALTER TABLE IF EXISTS polizas DROP COLUMN IF EXISTS reservada_hasta;
