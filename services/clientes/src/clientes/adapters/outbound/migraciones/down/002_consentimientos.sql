-- Reversa de 002_consentimientos.sql. migrar() no recorre esta carpeta: se aplica a mano.
-- Elimina los datos de consentimiento, idempotencia y outbox.
DROP TRIGGER IF EXISTS consentimiento_inmutable ON consentimiento;
DROP FUNCTION IF EXISTS consentimiento_inmutable();
DROP FUNCTION IF EXISTS fin_de_vigencia(TIMESTAMPTZ, INT);
DROP TABLE IF EXISTS outbox;
DROP TABLE IF EXISTS consentimiento_idempotencia;
DROP TABLE IF EXISTS consentimiento;
