-- Reversa de 006_listado_cotizaciones.sql. migrar() no recorre esta carpeta: se aplica a mano.
-- Solo quita índices: ningún dato cambia y el listado sigue funcionando (más lento).
DROP INDEX IF EXISTS cotizaciones_socio;
DROP INDEX IF EXISTS ofertas_vigente_hasta;
DROP INDEX IF EXISTS ofertas_creada_en_cotizacion;
