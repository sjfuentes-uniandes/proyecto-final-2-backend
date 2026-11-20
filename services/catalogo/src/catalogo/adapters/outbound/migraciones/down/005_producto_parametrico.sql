-- Reversa de 005_producto_parametrico.sql (HU-W21). Solo para pruebas de migración y retrocesos
-- planificados: se pierde la regla paramétrica de los productos que la tenían.
ALTER TABLE IF EXISTS productos DROP COLUMN IF EXISTS parametrico;
