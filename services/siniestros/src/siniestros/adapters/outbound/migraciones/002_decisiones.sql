-- Decisiones del analista (HU-W20; DS3-12 y DS3-14). Idempotente: migrar() lo aplica en cada arranque.
-- No repite lo que ya trae 001: valor_reclamado, moneda, decidido_en, pausado_desde,
-- pausa_acumulada_s y plazo_soporte_vence_en. La idempotencia usa la tabla `idempotencia` de W19
-- (clave `decision:<Idempotency-Key>`), no una columna propia.

-- Una fila por decisión registrada. Inmutable: la decisión es evidencia (mismo disparador de solo
-- inserción que historial_siniestro, que ya existe y nombra la tabla en su mensaje).
CREATE TABLE IF NOT EXISTS decisiones_siniestro (
  id uuid PRIMARY KEY,
  siniestro_id uuid NOT NULL REFERENCES siniestros (id),
  tipo text NOT NULL CHECK (tipo IN ('APROBAR', 'RECHAZAR', 'SOLICITAR_SOPORTE')),
  -- La clave tal como llegó; una clave, una decisión.
  idempotency_key uuid NOT NULL UNIQUE,
  huella_solicitud text NOT NULL CHECK (btrim(huella_solicitud) <> ''),
  -- Aprobar: liquidación y referenciaPago. Rechazar: causal y explicacionCliente. Soporte:
  -- documentos, canal, plazoDiasHabiles, plazoVenceEn y mensajeCliente.
  detalle jsonb NOT NULL,
  actor_id text NOT NULL CHECK (btrim(actor_id) <> ''),
  registrada_en timestamptz NOT NULL
);
COMMENT ON COLUMN decisiones_siniestro.detalle IS
  'sensible: lleva explicacionCliente y mensajeCliente (texto para el cliente); nunca a logs, eventos ni historial';
CREATE INDEX IF NOT EXISTS decisiones_por_siniestro ON decisiones_siniestro (siniestro_id, registrada_en);
-- Una sola decisión final (aprobar o rechazar) por siniestro: la segunda barrera de AC4, detrás del
-- FOR UPDATE del caso. Pueden existir varios pedidos de soporte.
CREATE UNIQUE INDEX IF NOT EXISTS decisiones_una_final ON decisiones_siniestro (siniestro_id)
  WHERE tipo IN ('APROBAR', 'RECHAZAR');

DROP TRIGGER IF EXISTS decisiones_siniestro_solo_insercion ON decisiones_siniestro;
CREATE TRIGGER decisiones_siniestro_solo_insercion BEFORE UPDATE OR DELETE ON decisiones_siniestro
  FOR EACH ROW EXECUTE FUNCTION historial_siniestro_solo_insercion();

-- La liquidación aprobada (nula hasta aprobar). La llena W20 al aprobar y W22 al crear un paramétrico.
ALTER TABLE siniestros ADD COLUMN IF NOT EXISTS deducible numeric(18, 2) NULL CHECK (deducible >= 0);
ALTER TABLE siniestros ADD COLUMN IF NOT EXISTS valor_indemnizar numeric(18, 2) NULL
  CHECK (valor_indemnizar > 0);

-- Invariantes del estado, de la misma familia que las de 001 (R-22 de W19): la base también las exige.
DO $$
BEGIN
  -- Un trámite nunca queda en el limbo: el caso que espera un soporte tiene plazo, y solo él.
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'siniestros_plazo_segun_estado') THEN
    ALTER TABLE siniestros ADD CONSTRAINT siniestros_plazo_segun_estado
      CHECK ((estado = 'FALTA_SOPORTE') = (plazo_soporte_vence_en IS NOT NULL));
  END IF;
  -- La liquidación existe si y solo si el caso está aprobado; el deducible no existe sin ella.
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'siniestros_liquidacion_segun_estado') THEN
    ALTER TABLE siniestros ADD CONSTRAINT siniestros_liquidacion_segun_estado
      CHECK ((estado = 'APROBADO') = (valor_indemnizar IS NOT NULL)
             AND (deducible IS NULL OR valor_indemnizar IS NOT NULL));
  END IF;
END
$$;

-- La tarea de plazos (DS3-13) busca los pedidos de soporte vencidos.
CREATE INDEX IF NOT EXISTS siniestros_plazo_soporte ON siniestros (plazo_soporte_vence_en)
  WHERE estado = 'FALTA_SOPORTE';
