-- Reversa de 005_decisiones_fallback.sql. migrar() no recorre esta carpeta: se aplica a mano.
-- Pierde el registro local de las decisiones de fallback (HU-W06); el evento ya publicado sigue en
-- auditoría. Va antes de down/004_ofertas.sql y down/003_cotizaciones.sql (referencia cotizaciones).
DROP TABLE IF EXISTS decisiones_fallback;
