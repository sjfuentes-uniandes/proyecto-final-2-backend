-- Alta de clientes por un asesor (HU-W30): el cliente se identifica por su documento y nace
-- PENDIENTE. El UNIQUE sobre el documento evita crear un segundo cliente al repetir el alta.
-- Se guarda el resultado, el proveedor y la referencia de la verificación; nunca biometría.
-- migrar() corre en cada arranque: todo es idempotente.
ALTER TABLE cliente ADD COLUMN IF NOT EXISTS tipo_documento   TEXT;
ALTER TABLE cliente ADD COLUMN IF NOT EXISTS numero_documento TEXT;
ALTER TABLE cliente ADD COLUMN IF NOT EXISTS nombre_completo  TEXT;
ALTER TABLE cliente ADD COLUMN IF NOT EXISTS fecha_nacimiento DATE;
ALTER TABLE cliente ADD COLUMN IF NOT EXISTS proveedor        TEXT;
ALTER TABLE cliente ADD COLUMN IF NOT EXISTS referencia       TEXT;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'cliente_origen_chk') THEN
        -- Un cliente nace con `sub`, con `registro_id` o con documento.
        ALTER TABLE cliente DROP CONSTRAINT IF EXISTS cliente_check;
        ALTER TABLE cliente ADD CONSTRAINT cliente_origen_chk
            CHECK (sub IS NOT NULL OR registro_id IS NOT NULL OR numero_documento IS NOT NULL);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'cliente_documento_completo_chk') THEN
        ALTER TABLE cliente ADD CONSTRAINT cliente_documento_completo_chk CHECK (
            numero_documento IS NULL
            OR (tipo_documento IS NOT NULL AND nombre_completo IS NOT NULL
                AND fecha_nacimiento IS NOT NULL)
        );
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS cliente_documento_uq
    ON cliente (tipo_documento, numero_documento);
