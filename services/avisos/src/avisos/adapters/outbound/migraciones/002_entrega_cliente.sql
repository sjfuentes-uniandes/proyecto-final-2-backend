-- cliente_id en la propia entrega (HU-M11): `reclamar` lo compara contra el cliente_id actual del
-- dispositivo para no reclamar, mientras esté ACTIVO, una entrega de un dueño anterior (baja o
-- reasignación). Un dispositivo INVALIDO sigue reclamable para cualquier cliente: así se cierra a
-- TOKEN_INVALIDO sin importar quién lo creó. Backfill desde el dispositivo para filas existentes.
ALTER TABLE entrega ADD COLUMN IF NOT EXISTS cliente_id uuid;

UPDATE entrega e SET cliente_id = d.cliente_id
FROM dispositivo d
WHERE e.dispositivo_id = d.id AND e.cliente_id IS NULL;

ALTER TABLE entrega ALTER COLUMN cliente_id SET NOT NULL;
