-- El reporte definitivo conserva los metadatos en ubicacion_declarada (jsonb).
ALTER TABLE borradores ADD COLUMN IF NOT EXISTS ubicacion jsonb NULL
  CHECK (ubicacion IS NULL OR (
    jsonb_typeof(ubicacion) = 'object'
    AND ubicacion ?& ARRAY['permiso', 'estado']
    AND ubicacion->>'permiso' IN
      ('PRECISO', 'APROXIMADO', 'DENEGADO', 'DENEGADO_PERMANENTEMENTE', 'NO_SOLICITADO')
    AND ubicacion->>'estado' IN ('DISPONIBLE', 'NO_CONFIABLE', 'NO_DISPONIBLE')
  ));
