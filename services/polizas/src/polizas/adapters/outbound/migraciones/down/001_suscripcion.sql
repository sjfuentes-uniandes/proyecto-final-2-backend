-- Reversa de 001_suscripcion.sql (HU-W13). Solo para pruebas de migración y retrocesos
-- planificados: borra las decisiones y el outbox.
DROP TABLE IF EXISTS outbox;
DROP TABLE IF EXISTS decisiones_suscripcion;
DROP FUNCTION IF EXISTS decisiones_inmutables();
DROP SEQUENCE IF EXISTS decisiones_codigo;
