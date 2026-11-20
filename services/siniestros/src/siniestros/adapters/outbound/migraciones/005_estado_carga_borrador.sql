-- HU-M01: estado de la carga para recuperar borradores y confirmar los objetos en S3.
CREATE INDEX IF NOT EXISTS borradores_por_actor
  ON borradores (actor_id, creado_en DESC, id DESC);

ALTER TABLE borrador_evidencias
  ADD COLUMN IF NOT EXISTS estado_carga text NOT NULL DEFAULT 'PENDIENTE';
ALTER TABLE borrador_evidencias
  ADD COLUMN IF NOT EXISTS carga_confirmada_en timestamptz;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'borrador_evidencias_estado_carga_valido'
      AND conrelid = 'borrador_evidencias'::regclass
  ) THEN
    ALTER TABLE borrador_evidencias
      ADD CONSTRAINT borrador_evidencias_estado_carga_valido
      CHECK (estado_carga IN ('PENDIENTE', 'CARGADA')) NOT VALID;
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'borrador_evidencias_confirmacion_coherente'
      AND conrelid = 'borrador_evidencias'::regclass
  ) THEN
    ALTER TABLE borrador_evidencias
      ADD CONSTRAINT borrador_evidencias_confirmacion_coherente
      CHECK (
        (estado_carga = 'PENDIENTE' AND carga_confirmada_en IS NULL)
        OR (estado_carga = 'CARGADA' AND carga_confirmada_en IS NOT NULL)
      ) NOT VALID;
  END IF;
END
$$;
