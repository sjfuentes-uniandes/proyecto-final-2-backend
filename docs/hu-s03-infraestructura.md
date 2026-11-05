# HU-S03: infraestructura de soporte del Sprint 3

Habilitadora del Sprint 3. Agrega a la plataforma, solo con Terraform y el catálogo, los servicios, colas, bucket, grupo, entrada de telemetría y ambiente de experimentos que usan las historias del sprint. Detalle de los recursos, los costos y la seguridad: [infra/PLATAFORMA.md, Sprint 3](../infra/PLATAFORMA.md#sprint-3-hu-s03). Despliegue, humo y ambiente `exp`: [infra/DESPLIEGUE.md](../infra/DESPLIEGUE.md), secciones 3.1 y 5.6.

## Qué recibe cada servicio

Los esqueletos (`services/siniestros`, `services/pagos-parametricos`, `services/adaptador-telemetria`) solo exponen `GET /health`, con `instrumentar` y la base por servicio. Su `config.py` ya lee las variables que les entrega la infraestructura:

| Servicio | Variables de entorno (infra/apps/ecs.tf) | Permisos |
| --- | --- | --- |
| `siniestros` | `DB_*`, `EVENTS_TOPIC_ARN` + `OUTBOX_ENABLED`, `SQS_SINIESTROS_INDEMNIZACIONES_URL`, `EVIDENCIAS_BUCKET`, `EVIDENCIAS_BUCKET_SSE` | `sns:Publish`; consumir su cola; `PutObject`, `GetObject` y `GetObjectVersion` en el bucket de evidencias, salvo `telemetria/` |
| `pagos-parametricos` | `DB_*`, `EVENTS_TOPIC_ARN` + `OUTBOX_ENABLED`, `SQS_PARAMETRICOS_EVENTOS_URL`, `SQS_PARAMETRICOS_POLIZAS_URL`, `SINIESTROS_URL` | `sns:Publish`; consumir sus dos colas |
| `adaptador-telemetria` | `DB_*` (Outbox), `EVENTS_TOPIC_ARN` + `OUTBOX_ENABLED`, `ALLY_TELEMETRIA_CREDENTIALS` (`{"hmac_secret": ...}`), `EVIDENCIAS_BUCKET`, `EVIDENCIAS_BUCKET_SSE`, `TELEMETRIA_ARCHIVO_PREFIJO` (`telemetria/`) | `sns:Publish`; `PutObject` solo en `telemetria/*` |
| `dinero` | además `SQS_DINERO_INDEMNIZACIONES_URL` | consumir `dinero-indemnizaciones` |
| `bff-web`, `bff-movil` | además `SINIESTROS_URL` | — (ningún BFF accede al bucket) |

Cada `PutObject` debe enviar `ServerSideEncryption = EVIDENCIAS_BUCKET_SSE`: la política del bucket rechaza cualquier otro. La entrada `POST /telemetria/v1/eventos` llega al adaptador en `POST /v1/eventos`, sin JWT y con el cuerpo intacto para validar la firma.

En local, `compose.yaml` levanta los tres servicios (puertos 8091, 8092 y 8093) con sus bases (`scripts/dev/postgres-init.sql`; si `pgdata` ya existe, `docker compose down -v` una vez).

## Colas

| Cola | Filtro `eventType` | Consumidor |
| --- | --- | --- |
| `parametricos-eventos` | `EventoParametricoObservado` | `pagos-parametricos` |
| `parametricos-polizas` | prefijo `Poliza` | `pagos-parametricos` |
| `dinero-indemnizaciones` | `SiniestroAprobado` | `dinero` |
| `siniestros-indemnizaciones` | prefijo `Indemnizacion` | `siniestros` |
| `consulta-proyeccion` (cambia) | prefijos `Poliza`, `Siniestro` e `Indemnizacion` | `consulta` |

Los nombres de evento fijan el contrato de enrutamiento: un evento con otro nombre no llega a la cola. Los esquemas de los eventos (`contracts/events`) y las operaciones medidas (`contracts/operaciones.md`) los define cada historia.

## Qué queda desbloqueado

| Integrante | Historias | Qué puede usar desde ya |
| --- | --- | --- |
| capbmaster | HU-W19 a HU-W23 | Esqueletos de `siniestros`, `pagos-parametricos` y `adaptador-telemetria`; bases, colas, bucket con `tmp/` y `telemetria/`, secreto HMAC, entrada de telemetría, grupo `analistas-siniestros` (asignable desde el portal) y `SINIESTROS_URL` en ambos BFF |
| npaezt | HU-M01 a HU-M05 | `bff-movil` → `siniestros` y la proyección con `Indemnizacion*` (HU-M05) |
| Santi-MISO | HU-W07 a HU-W09, HU-W25 | Ambiente `exp` (sin Spot, 2 réplicas, límites amplios), `pagos-parametricos` como worker que escala por la suma de sus dos colas y la fila Siniestros del tablero |

## Pruebas

| Nivel | Comando | Qué cubre |
| --- | --- | --- |
| N1 Terraform | `make infra-validar` | fmt, validate, `terraform test` de `platform` y `apps` (runs `sprint3`) y de `envs/exp` (`tests/exp`), y Trivy |
| Unitarias | `make test-unit SERVICE=<servicio>` | Arranque y salud de los tres esqueletos |
| N2 Humo | `make infra-probar ENV=int SMOKE_DLQ_COLA=dinero-indemnizaciones` | Pasos 10 a 12 y DLQ de una cola nueva (DESPLIEGUE.md 5.6) |
