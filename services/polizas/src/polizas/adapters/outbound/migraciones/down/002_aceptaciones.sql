-- Reversa de 002_aceptaciones.sql (HU-W15). Solo para pruebas de migración y retrocesos
-- planificados: borra las aceptaciones (sus filas de outbox quedan, como las de 001). Se aplica
-- antes que la reversa de 001, por la llave hacia decisiones_suscripcion.
DROP TABLE IF EXISTS aceptaciones;
DROP FUNCTION IF EXISTS aceptaciones_inmutables();
