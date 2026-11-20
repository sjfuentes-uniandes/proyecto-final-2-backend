-- Reversa de 007_cobertura_parametrica.sql. migrar() no recorre esta carpeta: se aplica a mano.
-- Pierde la instantánea: las pólizas que se emitan después salen sin coberturaParametrica.
ALTER TABLE cotizaciones DROP COLUMN IF EXISTS cobertura_parametrica;
