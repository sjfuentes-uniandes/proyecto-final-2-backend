-- Índices del listado de cotizaciones con oferta ("Cotizaciones abiertas" del portal). Aditiva e
-- idempotente: migrar() la aplica en cada arranque. Sin tablas ni columnas nuevas.
-- El listado ordena por (creada_en, cotizacion_id) descendente y pagina por cursor; un SOCIO solo
-- lee las suyas (socio_id).
CREATE INDEX IF NOT EXISTS ofertas_creada_en_cotizacion ON ofertas (creada_en DESC, cotizacion_id DESC);
CREATE INDEX IF NOT EXISTS ofertas_vigente_hasta ON ofertas (vigente_hasta);
CREATE INDEX IF NOT EXISTS cotizaciones_socio ON cotizaciones (socio_id) WHERE socio_id IS NOT NULL;
