-- HU-M01 (migración 003): borrador asociado al titular y póliza, con metadatos de cargas temporales en S3.
CREATE TABLE IF NOT EXISTS borradores (
  id uuid PRIMARY KEY,
  actor_id text NOT NULL,
  cliente_id uuid NOT NULL,
  poliza_id uuid NOT NULL,
  copia_poliza jsonb NOT NULL,
  creado_en timestamptz NOT NULL,
  correlation_id text NOT NULL
);
CREATE INDEX IF NOT EXISTS borradores_por_cliente ON borradores (cliente_id, creado_en);
CREATE INDEX IF NOT EXISTS borradores_por_poliza ON borradores (poliza_id, creado_en);

CREATE TABLE IF NOT EXISTS borrador_evidencias (
  id uuid PRIMARY KEY,
  borrador_id uuid NOT NULL REFERENCES borradores (id) ON DELETE CASCADE,
  nombre_archivo text NOT NULL CHECK (btrim(nombre_archivo) <> '' AND length(nombre_archivo) <= 255),
  tipo text NOT NULL CHECK (tipo IN ('FOTOGRAFIA', 'VIDEO')),
  clave_objeto text NOT NULL UNIQUE CHECK (btrim(clave_objeto) <> ''),
  sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  capturada_en timestamptz NULL,
  tamano_bytes bigint NOT NULL CHECK (tamano_bytes > 0),
  tipo_contenido text NOT NULL,
  duracion_segundos int NULL CHECK (duracion_segundos > 0),
  CHECK ((tipo = 'VIDEO') = (duracion_segundos IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS borrador_evidencias_por_borrador
  ON borrador_evidencias (borrador_id, capturada_en, id);
