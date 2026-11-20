-- Reversa de 001_telemetria.sql (HU-W21). Solo para pruebas de migración y retrocesos
-- planificados: se pierde el registro de eventos recibidos (un reenvío ya no se reconocería como
-- duplicado) y los eventos del Outbox que aún no se publicaron.
DROP TABLE IF EXISTS inbox;
DROP TABLE IF EXISTS outbox;
DROP TABLE IF EXISTS eventos_recibidos;
