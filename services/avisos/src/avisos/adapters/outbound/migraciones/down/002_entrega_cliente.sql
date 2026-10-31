-- Reversa de 002_entrega_cliente.sql. migrar() no recorre esta carpeta: se aplica a mano.
ALTER TABLE entrega DROP COLUMN IF EXISTS cliente_id;
