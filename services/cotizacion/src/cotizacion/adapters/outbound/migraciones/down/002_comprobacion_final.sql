-- Reversa de 002_comprobacion_final.sql. migrar() no recorre esta carpeta: se aplica a mano.
-- Pierde la comprobación final (y con ella la marca de revocación durante el perfilamiento).
ALTER TABLE perfiles_riesgo DROP COLUMN IF EXISTS comprobacion_final;
