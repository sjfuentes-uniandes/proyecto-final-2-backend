-- Emisión de la póliza (HU-W17, 17.8; ADR 0014, decisión 1). Aditiva e idempotente: migrar() la
-- aplica en cada arranque.
--
-- correlation_id: el del RecaudoConfirmado que creó la póliza (nulo si llegó sin uno válido, y en
-- las filas anteriores a 005). Lo propaga la tarea emisora a adaptador-firma y lo lleva
-- PolizaEmitida. Es identidad: el repositorio solo lo escribe al crear. La forma es la de
-- solventa_common.correlation.
ALTER TABLE polizas ADD COLUMN IF NOT EXISTS correlation_id text NULL;

DO $$BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'polizas_correlation_id') THEN
    ALTER TABLE polizas ADD CONSTRAINT polizas_correlation_id
      CHECK (correlation_id ~ '^[A-Za-z0-9._:-]{1,128}$');
  END IF;
END$$;

-- reservada_hasta (17.8, ajuste): la tarea emisora reserva la póliza antes de procesarla, en una
-- transacción corta, y la libera al terminar (al quedar ACTIVA, en la misma escritura). Mientras
-- está vigente, ninguna otra tarea la toma: dos tareas que suben la misma clave dejarían
-- referenciada una versión que deja de ser la actual, y el ciclo de vida del bucket expira las
-- versiones no actuales (infra/platform/storage.tf:132-133, 90 días). Una tarea que cae sin
-- liberarla la deja hasta que vence. El disparador de 004 ya permite cambiarla en una EMITIENDO
-- (no es identidad ni punto de control).
ALTER TABLE polizas ADD COLUMN IF NOT EXISTS reservada_hasta timestamptz NULL;

-- Las pólizas que la tarea emisora toma en cada ciclo (PolizasPg.pendientes): solo las EMITIENDO,
-- que son pocas y de vida corta; las ACTIVA, que crecen sin tope, no entran en el índice. La
-- reserva y el fallo se filtran sobre esas pocas filas.
CREATE INDEX IF NOT EXISTS polizas_emitiendo ON polizas (actualizada_en) WHERE estado = 'EMITIENDO';
