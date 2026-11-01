-- Reversa de 002_indices_estado_polizas.sql (HU-W18). Solo borra índices; no toca datos.
DROP INDEX IF EXISTS poliza_cliente_vigencia_hasta_idx;
DROP INDEX IF EXISTS poliza_cliente_ramo_idx;
DROP INDEX IF EXISTS poliza_cliente_emitida_en_idx;
DROP INDEX IF EXISTS poliza_cliente_proyectada_en_idx;
