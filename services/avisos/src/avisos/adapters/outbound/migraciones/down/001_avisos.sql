-- Reversa de 001_avisos.sql (HU-M11). Solo para pruebas de migración y retrocesos planificados:
-- borra dispositivos, entregas e inbox (los eventos ya consumidos no vuelven de la cola).
DROP TABLE IF EXISTS entrega;
DROP TABLE IF EXISTS dispositivo;
DROP TABLE IF EXISTS inbox;
