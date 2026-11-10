-- Bases locales por servicio (una por servicio, como en RDS). Las crea la imagen de Postgres
-- solo al inicializar un volumen vacío: si pgdata ya existe, `docker compose down -v` una vez.
CREATE DATABASE api_socios;
CREATE DATABASE clientes;
CREATE DATABASE catalogo;
CREATE DATABASE cotizacion;
CREATE DATABASE polizas;
CREATE DATABASE consulta;
-- HU-S03: siniestros, pagos-parametricos (Inbox y coberturas) y adaptador-telemetria (Outbox).
CREATE DATABASE siniestros;
CREATE DATABASE pagos_parametricos;
CREATE DATABASE adaptador_telemetria;
-- HU-W24: auditoria (Inbox y cabeza de la cadena de lotes).
CREATE DATABASE auditoria;
-- HU-W23: dinero (recaudos e indemnizaciones, Outbox e Inbox). Un volumen que ya existe no la tiene:
-- `docker compose down -v` una vez, o `CREATE DATABASE dinero;` a mano.
CREATE DATABASE dinero;
