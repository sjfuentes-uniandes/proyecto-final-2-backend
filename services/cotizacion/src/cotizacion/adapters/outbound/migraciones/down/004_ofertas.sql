-- Reversa de 004_ofertas.sql. migrar() no recorre esta carpeta: se aplica a mano.
-- Pierde las ofertas (HU-W04); las cotizaciones no cambian y vuelven a la vigencia de HU-W13.
-- Va antes de down/003_cotizaciones.sql (las ofertas referencian las cotizaciones).
DROP TABLE IF EXISTS ofertas;
DROP SEQUENCE IF EXISTS ofertas_codigo_seq;
