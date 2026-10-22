-- Bases locales por servicio (una por servicio, como en RDS). Las crea la imagen de Postgres
-- solo al inicializar un volumen vacío: si pgdata ya existe, `docker compose down -v` una vez.
CREATE DATABASE api_socios;
CREATE DATABASE clientes;
CREATE DATABASE catalogo;
CREATE DATABASE cotizacion;
CREATE DATABASE polizas;
