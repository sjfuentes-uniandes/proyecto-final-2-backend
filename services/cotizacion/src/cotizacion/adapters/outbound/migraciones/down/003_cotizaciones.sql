-- Reversa de 003_cotizaciones.sql. migrar() no recorre esta carpeta: se aplica a mano.
-- Pierde las cotizaciones calculadas (HU-W03); los perfiles no cambian.
DROP TABLE IF EXISTS cotizaciones;
