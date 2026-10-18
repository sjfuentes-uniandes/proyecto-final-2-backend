-- Reversa de 003_cliente_documento.sql. migrar() no recorre esta carpeta: se aplica a mano.
-- Elimina los clientes dados de alta por documento (no tienen `sub` ni `registro_id`) y las
-- columnas del documento y de la traza de la verificación.
DROP INDEX IF EXISTS cliente_documento_uq;
ALTER TABLE cliente DROP CONSTRAINT IF EXISTS cliente_documento_completo_chk;
ALTER TABLE cliente DROP CONSTRAINT IF EXISTS cliente_origen_chk;
DELETE FROM cliente WHERE sub IS NULL AND registro_id IS NULL;
ALTER TABLE cliente ADD CONSTRAINT cliente_check CHECK (sub IS NOT NULL OR registro_id IS NOT NULL);
ALTER TABLE cliente DROP COLUMN IF EXISTS referencia;
ALTER TABLE cliente DROP COLUMN IF EXISTS proveedor;
ALTER TABLE cliente DROP COLUMN IF EXISTS fecha_nacimiento;
ALTER TABLE cliente DROP COLUMN IF EXISTS nombre_completo;
ALTER TABLE cliente DROP COLUMN IF EXISTS numero_documento;
ALTER TABLE cliente DROP COLUMN IF EXISTS tipo_documento;
