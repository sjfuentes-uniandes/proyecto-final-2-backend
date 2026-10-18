-- Comprobación final del consentimiento tras consultar las fuentes (HU-W31, ADR 0011 §3).
-- Aditiva y nullable: los perfiles anteriores quedan sin comprobación. Mismo formato que
-- `consentimiento`. Idempotente: migrar() la aplica en cada arranque.
ALTER TABLE perfiles_riesgo ADD COLUMN IF NOT EXISTS comprobacion_final jsonb NULL;
