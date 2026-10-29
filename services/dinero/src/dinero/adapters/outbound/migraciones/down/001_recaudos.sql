-- Reversa de 001_recaudos.sql (HU-W16). Solo para pruebas de migración y retrocesos planificados:
-- borra los movimientos, el outbox y el inbox (el disparador cae con su tabla).
DROP TABLE IF EXISTS inbox;
DROP TABLE IF EXISTS outbox;
DROP TABLE IF EXISTS recaudos;
DROP FUNCTION IF EXISTS recaudos_protegidos();
