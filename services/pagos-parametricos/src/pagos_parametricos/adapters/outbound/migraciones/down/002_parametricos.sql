-- Reversa de 002_parametricos.sql (HU-W21). Solo para pruebas de migración y retrocesos
-- planificados: se pierden la copia de coberturas, las evaluaciones y las activaciones (los eventos
-- ya evaluados se volverían a evaluar al reprocesarse). El outbox no se borra: es de 001.
DROP TABLE IF EXISTS activaciones;
DROP TABLE IF EXISTS evaluaciones;
DROP TABLE IF EXISTS eventos_evaluados;
DROP TABLE IF EXISTS coberturas_activas;
DROP TABLE IF EXISTS inbox;
