-- Reversa de 001_poliza_cliente.sql (HU-M10). Solo para pruebas de migración y retrocesos
-- planificados: borra la proyección y el inbox (los eventos ya consumidos no vuelven de la cola).
DROP TABLE IF EXISTS poliza_cliente;
DROP TABLE IF EXISTS inbox;
