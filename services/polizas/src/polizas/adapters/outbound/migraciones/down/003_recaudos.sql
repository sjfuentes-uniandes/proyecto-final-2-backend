-- Reversa de 003_recaudos.sql (HU-W16, 16.13). Solo para pruebas de migración y retrocesos
-- planificados: borra la proyección del recaudo y el inbox (los eventos ya consumidos no vuelven
-- de la cola). Se aplica antes que la de 002.
DROP TABLE IF EXISTS recaudos_aceptacion;
DROP TABLE IF EXISTS inbox;
