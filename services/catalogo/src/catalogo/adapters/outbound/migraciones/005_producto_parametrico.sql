-- Bloque paramétrico opcional del producto (HU-W21, W21-B04). Idempotente: migrar() lo aplica en
-- cada arranque. Es aditivo: los productos existentes quedan con NULL y su API no cambia.
-- Forma: {tipoEvento, unidad, umbral, valorPago, versionRegla}; la valida el dominio al sembrar.
ALTER TABLE productos ADD COLUMN IF NOT EXISTS parametrico jsonb NULL;
