-- HU-M01: un borrador conserva su confirmacion, aunque cambie despues el estado del siniestro.
ALTER TABLE borradores ADD COLUMN IF NOT EXISTS causa text NULL
  CHECK (causa IS NULL OR (btrim(causa) <> '' AND length(causa) <= 64));
ALTER TABLE borradores ADD COLUMN IF NOT EXISTS fecha_evento timestamptz NULL;
ALTER TABLE borradores ADD COLUMN IF NOT EXISTS version int NOT NULL DEFAULT 1 CHECK (version >= 1);
ALTER TABLE borradores ADD COLUMN IF NOT EXISTS estado text NOT NULL DEFAULT 'ABIERTO'
  CHECK (estado IN ('ABIERTO', 'CONVERTIDO'));
ALTER TABLE borradores ADD COLUMN IF NOT EXISTS siniestro_id uuid NULL UNIQUE REFERENCES siniestros (id);
ALTER TABLE borradores ADD COLUMN IF NOT EXISTS envio_resultado jsonb NULL;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'borrador_conversion_completa'
                 AND conrelid = 'borradores'::regclass) THEN
    ALTER TABLE borradores ADD CONSTRAINT borrador_conversion_completa CHECK (
      (estado = 'ABIERTO' AND siniestro_id IS NULL AND envio_resultado IS NULL)
      OR (estado = 'CONVERTIDO' AND siniestro_id IS NOT NULL AND envio_resultado IS NOT NULL)
    );
  END IF;
END $$;
ALTER TABLE siniestros ADD COLUMN IF NOT EXISTS descripcion text NULL
  CHECK (descripcion IS NULL OR (btrim(descripcion) <> '' AND length(descripcion) <= 2000));
ALTER TABLE evidencias ADD COLUMN IF NOT EXISTS duracion_segundos int NULL CHECK (duracion_segundos > 0);
CREATE INDEX IF NOT EXISTS borradores_abiertos_por_actor
  ON borradores (actor_id, creado_en DESC, id DESC) WHERE estado = 'ABIERTO';
CREATE INDEX IF NOT EXISTS siniestros_por_cliente
  ON siniestros (cliente_id, reportado_en DESC, id DESC);
