-- Un cliente por titular de Cognito (`sub`) o, antes de que exista la cuenta, por `registro_id`:
-- el UNIQUE evita crear un segundo cliente al reintentar la verificación. `sub` queda vacío
-- hasta el primer ingreso. No se guarda biometría: solo el veredicto de la identidad.
CREATE TABLE IF NOT EXISTS cliente (
    id            UUID PRIMARY KEY,
    sub           TEXT UNIQUE,
    registro_id   UUID UNIQUE,
    correo        TEXT,
    estado        TEXT NOT NULL CHECK (estado IN ('VERIFICADO', 'PENDIENTE', 'RECHAZADO')),
    verificado_en TIMESTAMPTZ,
    creado        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (sub IS NOT NULL OR registro_id IS NOT NULL)
);
