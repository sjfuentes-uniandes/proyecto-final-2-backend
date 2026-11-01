-- Índices para el estado y el listado de pólizas (HU-W18). Sin cambios de columnas; idempotente.
CREATE INDEX IF NOT EXISTS poliza_cliente_vigencia_hasta_idx ON poliza_cliente (vigencia_hasta);
CREATE INDEX IF NOT EXISTS poliza_cliente_ramo_idx ON poliza_cliente (ramo);
CREATE INDEX IF NOT EXISTS poliza_cliente_emitida_en_idx ON poliza_cliente (emitida_en DESC, poliza_id);
CREATE INDEX IF NOT EXISTS poliza_cliente_proyectada_en_idx ON poliza_cliente (proyectada_en);
