-- Reversa de 004_polizas.sql (HU-W17, 17.6). Solo para pruebas de migración y retrocesos
-- planificados: borra las pólizas y su secuencia de números. Se aplica antes que la de 002, por la
-- llave hacia aceptaciones.
DROP TABLE IF EXISTS polizas;
DROP FUNCTION IF EXISTS polizas_protegidas();
DROP SEQUENCE IF EXISTS polizas_numero;
