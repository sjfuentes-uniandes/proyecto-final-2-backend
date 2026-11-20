-- Siniestros paramétricos (HU-W22; DS3-23). Idempotente: migrar() lo aplica en cada arranque.
-- `evento_id` y `regla` ya existen desde 001 (W19); aquí se agrega la medición que activó el
-- siniestro y la barrera de negocio contra el doble siniestro.

-- La medición del evento externo (`fuente`, `tipoEvento`, `valorObservado`, `umbral`, `unidad`,
-- `zona`, `observadoEn` y `eventoOrigen`). Nula en los siniestros de otro origen. `regla` guarda
-- {codigo, version}: la regla aplicada (DS3-21). Sin PII: solo la medición y la zona.
ALTER TABLE siniestros ADD COLUMN IF NOT EXISTS evento jsonb NULL;

-- Un solo siniestro paramétrico por evento y póliza (UNIQUE evento-póliza, modelo_concurrencia (9)).
-- Es la segunda barrera, detrás de la clave de idempotencia: aunque la clave se purgue o dos
-- réplicas lleguen a la vez, `INSERT ... ON CONFLICT DO NOTHING` deja un solo siniestro.
CREATE UNIQUE INDEX IF NOT EXISTS siniestros_parametrico_evento_poliza
  ON siniestros (evento_id, poliza_id) WHERE origen = 'PARAMETRICO';
