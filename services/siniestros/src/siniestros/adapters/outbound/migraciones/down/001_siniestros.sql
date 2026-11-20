-- Reversa de 001_siniestros.sql (HU-W19). Solo para pruebas de migración y retrocesos planificados:
-- borra los siniestros, sus evidencias y su historial, el outbox, el inbox, la idempotencia y la
-- secuencia de códigos (el disparador del historial cae con su tabla).
DROP TABLE IF EXISTS secuencia_codigo;
DROP TABLE IF EXISTS idempotencia;
DROP TABLE IF EXISTS inbox;
DROP TABLE IF EXISTS outbox;
DROP TABLE IF EXISTS historial_siniestro;
DROP TABLE IF EXISTS evidencias;
DROP TABLE IF EXISTS siniestros;
DROP FUNCTION IF EXISTS historial_siniestro_solo_insercion();
