DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'borradores' AND column_name = 'ubicacion'
  ) THEN
    LOCK TABLE borradores IN ACCESS EXCLUSIVE MODE;
    IF EXISTS (SELECT 1 FROM borradores WHERE ubicacion IS NOT NULL) THEN
      RAISE EXCEPTION 'No se puede revertir ubicacion_reporte: existen metadatos de ubicacion';
    END IF;
  END IF;
  IF to_regclass('public.siniestros') IS NOT NULL THEN
    LOCK TABLE siniestros IN ACCESS EXCLUSIVE MODE;
    IF EXISTS (SELECT 1 FROM siniestros WHERE ubicacion_declarada ? 'permiso') THEN
      RAISE EXCEPTION 'No se puede revertir ubicacion_reporte: existen metadatos de ubicacion';
    END IF;
  END IF;
END $$;
ALTER TABLE IF EXISTS borradores DROP COLUMN IF EXISTS ubicacion;
