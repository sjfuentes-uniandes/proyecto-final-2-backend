-- Reversa de 001_descartes.sql (HU-W24). Solo para pruebas de migración y retrocesos planificados:
-- los mensajes siguen en la DLQ; se pierden las causas anotadas y el historial de resoluciones.
-- El outbox no se borra: lo comparte HU-W21.
DROP TABLE IF EXISTS resoluciones_descarte;
DROP TABLE IF EXISTS fallos_consumo;
