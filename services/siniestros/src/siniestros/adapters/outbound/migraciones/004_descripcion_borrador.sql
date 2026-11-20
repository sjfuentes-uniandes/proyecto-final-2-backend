ALTER TABLE borradores ADD COLUMN IF NOT EXISTS descripcion text;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'borradores_descripcion_valida'
      AND conrelid = 'borradores'::regclass
  ) THEN
    ALTER TABLE borradores
      ADD CONSTRAINT borradores_descripcion_valida
      CHECK (
        descripcion IS NOT NULL
        AND btrim(descripcion) <> ''
        AND char_length(descripcion) <= 2000
      ) NOT VALID;
  END IF;
END
$$;
