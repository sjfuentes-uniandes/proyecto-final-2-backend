-- Instantánea de la cobertura paramétrica de la cotización (HU-W24): la regla del producto
-- paramétrico del catálogo (HU-W21) y la zona del riesgo de la solicitud. Aditiva e idempotente:
-- migrar() la aplica en cada arranque. NULL en los productos que no son paramétricos y en las
-- cotizaciones anteriores. La fila sigue siendo inmutable (cotizaciones_inmutables).
-- Forma: {tipoEvento, zona, umbral, unidad, valorPago, moneda, versionRegla}.
ALTER TABLE cotizaciones ADD COLUMN IF NOT EXISTS cobertura_parametrica jsonb NULL;
