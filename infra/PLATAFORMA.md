# Plataforma Solventa en AWS (historias en Ready)

Infraestructura para ejecutar en AWS las 13 historias de la columna **Ready** del backlog. Se basa en el modelo de despliegue (`docs/arquitectura/modelo_despliegue.puml`, `diagrama_despliegue_ecs_fargate.puml`), en la Entrega 8 de arquitectura (componentes, conectores y patrones).

| Raíz | Qué crea | Depende de |
| --- | --- | --- |
| `infra/platform/` | Red en 2 zonas, NAT instance (o NAT Gateway), KMS opcional, ECR, RDS PostgreSQL con base y usuario por servicio, clúster ECS y Service Connect, SNS/SQS/DLQ, S3 de auditoría con Object Lock, Cognito, ALB interno + VPC Link v2 + API Gateway REST + WAF, CloudFront para el portal y tópico de alertas | Nada |
| `infra/apps/` | Por cada servicio con imagen publicada: roles IAM, grupos de seguridad, tareas Fargate con colector ADOT, servicios ECS en capas, autoescalado, alarmas y tablero | Estado de `platform` e imágenes en ECR |

El catálogo de servicios vive en un solo lugar: `infra/platform/catalog.tf`. Allí se declaran nivel, puerto del listener del ALB, base de datos, dependencias síncronas, eventos, colas y aliados. `apps` lo recibe por el output `platform`.

## Arquitectura desplegada

```
Portal web ─► CloudFront + S3 (SPA)
Portal web / App móvil ─HTTPS─► WAF ─► API Gateway "canales" ─┐  (JWT Cognito clientes/back-office)
Socio ──HTTPS (+mTLS opc.)──► WAF ─► API Gateway "socios" ────┤  (JWT client_credentials + API key + plan de uso)
                                                              ▼
                                          VPC Link v2 ─► ALB interno (8081/8082/8083)
                                                              ▼
 ┌──────────────────────── ECS Fargate · subredes privadas zonas A y B ────────────────────────┐
 │ bff-web   bff-movil   api-socios          ◄─ capa de acceso                                  │
 │ clientes  catalogo    cotizacion          ◄─ núcleo (REST interno por ECS Service Connect)   │
 │ adaptador-datos  adaptador-identidad      ◄─ adaptadores aislados por aliado (vía NAT)       │
 │ auditoria (worker SQS)   simulador-aliados (WireMock, integración)                           │
 └──────────────────────────────────────────────────────────────────────────────────────────────┘
        │ SQL/TLS                    │ Outbox ─► SNS ─► SQS (+DLQ)        │ HTTPS
        ▼                            ▼                                    ▼
 RDS PostgreSQL (base por servicio)  auditoria / consentimientos-cotizacion  S3 auditoría (Object Lock)
 Secrets Manager + KMS · CloudWatch (logs, EMF, alarmas → SNS alertas) · X-Ray (ADOT)
```

## Trazabilidad historia → infraestructura

| Historia | Recursos |
| --- | --- |
| **HU-W01** Generación de credenciales | Pool Cognito `socios` con servidor de recursos `solventa` y permisos (`partner_scopes`). Un app client `client_credentials` por socio, con secreto generado por Cognito que se muestra una vez. `api-socios` tiene permisos IAM para crear, actualizar o eliminar clientes OAuth y API keys. Respuesta 401 para credenciales inválidas y 403 para permisos no asignados. Pool `backoffice` con grupo `administradores-socios` y MFA obligatorio. |
| **HU-W02** Cuota por cada socio | API REST `socios` con `api_key_required` y planes de uso (`partner_tiers`). El límite y la cuota se aplican **por API key**. 429 con `Retry-After`. Filtro de métricas `Solventa/Socios PartnerThrottled` por `ApiKeyId`. |
| **HU-W10** Datos autorizados | `adaptador-datos` con credenciales en Secrets Manager (`open-finance`, `datos-abiertos`), timeout configurable y salida por NAT. Base `cotizacion` para guardar datos mínimos y su procedencia. |
| **HU-W11** Cálculo perfil de riesgo | Servicios `cotizacion` y `catalogo`, cada uno con su base (versiones de reglas y perfiles). |
| **HU-W12** Consulta fuentes en paralelo | Un adaptador por grupo de aliados (*bulkhead*). `ALLY_*_TIMEOUT_MS` por dependencia. Trazas padre/hijas en X-Ray a través de ADOT. |
| **HU-W27** Correlación de métricas y trazas | API Gateway con X-Ray y logs de acceso JSON (`requestId`, `xrayTraceId`). Encabezado `X-Request-Id` inyectado en cada solicitud. Colector ADOT por tarea (OTLP → X-Ray + EMF). Tablero por ambiente. |
| **HU-W28** Generación de alertas | Tópico SNS `alertas` con correo. Las alarmas envían tanto `ALARM` como `OK`: p95 del perfilamiento > 400 ms, 5XX, p95 del API, destinos saludables, CPU/memoria, atraso de colas, DLQ y RDS. Los umbrales se configuran en variables. |
| **HU-W30** Verificación de identidad | Servicios `clientes` (base propia) y `adaptador-identidad` (secreto `kyc`, timeout propio para distinguir PENDIENTE de RECHAZADO). |
| **HU-W31** Verificación de consentimiento | `clientes` guarda los consentimientos y `cotizacion` los consulta antes de llamar al adaptador. La cola `consentimientos-cotizacion` invalida cachés cuando se revoca un consentimiento. Las decisiones quedan en la auditoría inmutable. |
| **HU-M06** Validación de cliente | Ruta `/movil/*` → `bff-movil` → `clientes` → `adaptador-identidad`. La regla WAF de tamaño de cuerpo queda en modo conteo para permitir la selfie. No se guarda biometría en AWS. |
| **HU-M07** Ingreso biométrico | Cliente Cognito `app-movil` (PKCE, `enable_token_revocation`). La biometría del sistema operativo desbloquea el refresh token; revocarlo obliga a una autenticación completa. |
| **HU-M08** Registro de autorización | `clientes` + Outbox → SNS → SQS `auditoria` → worker `auditoria` → S3 con Object Lock (versionado, KMS, solo TLS). |
| **HU-M09** Revocación de consentimiento | Igual que HU-M08, más la cola `consentimientos-cotizacion`. |

Transversal (ARQ-001/002/003): tareas repartidas en dos zonas con rebalanceo, circuito de despliegue con rollback, Service Connect, autoescalado (APIs por CPU y workers por profundidad de cola), RDS Multi-AZ con `high_availability = true`, cifrado con KMS y secretos fuera de las imágenes.

## Decisiones y costos (perfil por defecto: capa gratuita)

Ninguna configuración con ECS Fargate es 100 % gratuita, porque **Fargate no tiene capa gratuita**. WAF tampoco la tiene, pero se mantiene porque el documento de arquitectura lo exige en el borde. El perfil por defecto usa todo lo que sí tiene capa gratuita y apaga lo que no es indispensable:

| Componente | Perfil por defecto | Capa gratuita (cuentas anteriores a jul-2025, 12 meses) | Sin capa gratuita |
| --- | --- | --- | --- |
| Entrada privada | **ALB interno** + VPC Link v2 | 750 h/mes: 0 USD | ~18 USD/mes |
| Salida a Internet | **NAT instance `t3.micro`** (`egress_mode = "nat_instance"`) | 750 h de EC2 y de IPv4 pública: 0 USD | ~11 USD/mes |
| Base de datos | RDS `db.t4g.micro` Single-AZ, 20 GB, sin autoescalado de almacenamiento | 750 h + 20 GB: 0 USD | ~14 USD/mes |
| Tareas | 0,25 vCPU / 1 GB, **todas en FARGATE_SPOT** | Sin capa gratuita | ~3,2 USD/mes por tarea (~10,6 en FARGATE) |
| Cifrado | Claves administradas por AWS (`use_customer_managed_key = false`) | 0 USD | 0 USD (1 USD/mes con clave propia) |
| WAF | Web ACL mínimo: límite por IP + reglas comunes de AWS | Sin capa gratuita | ~7 USD/mes |
| Secrets Manager | 8 secretos (bases, aliados y maestro de RDS) | 30 días de prueba | ~3,2 USD/mes |
| Alarmas CloudWatch | ~34 con los 10 servicios | 10 gratis | ~2,4 USD/mes |
| API Gateway, Cognito (usuarios), CloudFront, S3, SNS, SQS, ECR, X-Ray | Volumen de pruebas | Dentro de la capa gratuita | Centavos |

Estimado con los 10 servicios encendidos 24/7:
- **Con capa gratuita: ≈ 45 USD/mes**, casi todo Fargate Spot y WAF.
- **Pausado (`paused = true` en apps): ≈ 13 USD/mes.** Pausar lleva todas las tareas a 0 sin destruir nada; también se puede detener RDS hasta 7 días (`aws rds stop-db-instance`).
- **Sin capa gratuita: ≈ 88 USD/mes encendido y ≈ 57 USD/mes pausado.**

En las cuentas creadas después del 15-jul-2025 (plan gratuito con créditos), estos costos se descuentan de los créditos del plan (hasta 200 USD durante 6 meses). Los clientes OAuth de socios (`client_credentials`) tienen un cobro propio en Cognito: revisar su precio antes de crear muchos socios.

**Qué se pierde con el perfil barato y cómo recuperarlo:**
- **NAT instance:** una sola instancia en la zona A. Si falla, EC2 la recupera, pero mientras tanto se corta la salida a Internet (aliados, Cognito y API Gateway desde `api-socios`). El ALB, RDS y Service Connect siguen funcionando. `egress_mode = "nat_gateway"` usa el servicio administrado (~33 USD/mes; uno por zona con `high_availability = true`).
- **FARGATE_SPOT:** AWS puede interrumpir una tarea con 2 minutos de aviso y ECS la reemplaza. `use_fargate_spot = false` para producción.
- **Una réplica y RDS Single-AZ:** `min_replicas = 2` deja una réplica activa por zona (HU-W29) y `high_availability = true` activa RDS Multi-AZ.

**¿Por qué hace falta NAT?** Las tareas Fargate y RDS viven en subredes privadas, sin IP pública. Aun así, las tareas necesitan conexiones **salientes** a:
- **Servicios de AWS con endpoint público:** ECR (descargar la imagen al arrancar), CloudWatch Logs, Secrets Manager (credenciales de la base), SNS/SQS (Outbox y consumidores), X-Ray (trazas) y, desde `api-socios`, Cognito y API Gateway (alta de socios, HU-W01).
- **Imágenes públicas:** el colector ADOT y el cliente `psql` vienen de `public.ecr.aws`.
- **Aliados externos:** KYC y Open Finance (HU-W10, HU-W30, HU-M06).

Sin salida, una tarea privada ni siquiera arranca, porque no puede descargar su imagen ni leer sus secretos. Hay tres formas de dar esa salida:

| Opción | Costo | Comentario |
| --- | --- | --- |
| **NAT instance** (por defecto) | 0 con capa gratuita; ~11 USD/mes sin ella | Todo sigue privado. Una sola instancia. |
| NAT Gateway | ~33 USD/mes por zona | Administrado y con alta disponibilidad. |
| Endpoints de interfaz | ~7 USD/mes por servicio y zona (~65 USD/mes para ~9) | No cubren aliados externos ni `public.ecr.aws`. |

La NAT solo permite conexiones que salen desde la VPC: nadie en Internet puede abrir una conexión hacia las tareas.

**¿Por qué no la VPC por defecto?** La VPC por defecto **no es privada**: todas sus subredes tienen ruta directa al Internet Gateway (son públicas) y no tiene subredes privadas ni aisladas. Para usarla sin NAT, cada tarea necesitaría IP pública (`assign_public_ip = true`; ~3,65 USD/mes por IPv4, ≈ 36 USD/mes con 10 servicios) y quedaría alcanzable desde Internet, protegida solo por su grupo de seguridad. Además, la VPC por defecto no se crea ni se destruye con Terraform, puede no existir o estar compartida con otras cosas de la cuenta, y su CIDR es fijo. Una VPC propia no cuesta nada: se pagan la NAT y los recursos, no la VPC ni sus subredes.

**Otras decisiones:**
- **API Gateway REST en lugar de HTTP API:** los planes de uso, las API keys y WAF solo existen en REST. REST se integra en privado con el **ALB** mediante un **VPC Link v2** (`integration_target`). El ALB tiene un listener por servicio de acceso (8081 `bff-web`, 8082 `bff-movil`, 8083 `api-socios`) y solo acepta tráfico del grupo de seguridad del VPC Link.
- **Dos APIs** (`canales` y `socios`): el mTLS aplica a todo un dominio. Separarlos permite exigir certificado solo a los socios y desactivar el endpoint `execute-api` del API de socios cuando existe dominio propio.
- **Endpoints de interfaz:** opcionales (`interface_endpoints`, ~7 USD/mes cada uno por zona). El endpoint *gateway* de S3 es gratuito y siempre se crea, así que las capas de ECR y la auditoría no pasan por la NAT.
- **Caché de catálogo (ElastiCache)**, **proyección CQRS**, **Saga** y **evidencias de siniestros** no se crean porque ninguna historia en Ready los usa. El catálogo permite agregarlos después.

## Despliegue

El despliegue, las actualizaciones por microservicio y la destrucción están automatizados con `make infra-*` y con GitHub Actions. Ver **[DESPLIEGUE.md](DESPLIEGUE.md)** para el orden, los pasos intermedios, la dependencia de imágenes y la configuración de CI.

```bash
make infra-bootstrap              # una vez por cuenta
make infra-desplegar ENV=int      # plataforma -> bases -> imágenes -> aplicaciones
make infra-destruir ENV=int       # todo el ambiente
```

## Contratos para los servicios

**Imágenes:** escuchan en el puerto `8080`, exponen `GET /health` e incluyen `/app/healthcheck`.

**Variables de entorno** (definidas en `infra/apps/ecs.tf`). En Python, `solventa_common.settings.ServiceSettings` ya las lee: el `config.py` de cada servicio hereda de ella y `settings.database_url` arma la conexión con `DB_*` en AWS o con el Postgres local en desarrollo.

| Variable | Servicios | Contenido |
| --- | --- | --- |
| `SERVICE_NAME`, `ENVIRONMENT`, `CORRELATION_HEADER`, `REQUEST_ID_HEADER` | Todos | Identidad del servicio y encabezados de correlación (`X-Correlation-Id`; si no llega, usar `X-Request-Id` de API Gateway) |
| `<DEPENDENCIA>_URL` | Según `calls` | `http://<servicio>:8080` por Service Connect, por ejemplo `CLIENTES_URL` |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_SSLMODE` | Con base | Base y usuario propios (secretos inyectados por ECS) |
| `EVENTS_TOPIC_ARN`, `OUTBOX_ENABLED` | Publicadores | Tópico de eventos de negocio |
| `SQS_<COLA>_URL` | Consumidores | Por ejemplo `SQS_AUDITORIA_URL` |
| `ALLY_<ALIADO>_URL`, `ALLY_<ALIADO>_TIMEOUT_MS`, `ALLY_<ALIADO>_CREDENTIALS` | Adaptadores | Endpoint, timeout y JSON de credenciales |
| `AUDIT_BUCKET` | auditoria | Bucket con Object Lock |
| `PARTNERS_USER_POOL_ID`, `PARTNERS_API_ID`, `PARTNERS_USAGE_PLANS`, `PARTNERS_TOKEN_URL` | api-socios | Alta de socios y asignación de plan |
| `JWT_ISSUERS` | Acceso | Emisores válidos para revalidar el token (defensa en profundidad) |
| `OTEL_*` | Todos | Exportación OTLP a `localhost:4317` (colector ADOT) |

**Encabezados que agrega API Gateway** a partir del token validado (sobrescriben lo que envíe el cliente): `X-Authenticated-Sub` (canales), `X-Partner-Client-Id`, `X-Partner-Scopes` y `X-Partner-Key-Id` (socios). `api-socios` responde 403 cuando el permiso requerido no está en `X-Partner-Scopes` (HU-W01 AC4).

**Eventos:** el relay del Outbox publica en SNS con el atributo de mensaje `eventType` (`String`), por ejemplo `ConsentimientoOtorgado`, `ConsentimientoRevocado` o `IdentidadVerificada`. El cuerpo es el sobre del evento con `eventId`, que los consumidores usan para la idempotencia. La cola `auditoria` recibe todos los eventos; `consentimientos-cotizacion` recibe solo los `Consentimiento*`.

**Métricas** (usadas por alarmas y tablero): espacio de nombres `Solventa`, dimensiones `Environment`, `Service` y `Operation`. Los servicios publican con OpenTelemetry:
- `OperationDuration`: histograma en ms. `OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION` ya lo configura como exponencial, lo que permite calcular p95.
- `OperationErrors`: contador.

El perfilamiento debe usar `Service=cotizacion` y `Operation=perfilamiento`.

**Logs:** JSON por línea con `service`, `operation`, `result`, `duration_ms` y `correlation_id`. Nunca incluir secretos, tokens, biometría ni payloads financieros completos (HU-W27 AC4).

## Probar la alerta sin carga (HU-W28 AC4)

```bash
aws cloudwatch put-metric-data --namespace Solventa --metric-name OperationDuration --unit Milliseconds \
  --dimensions Environment=int,Service=cotizacion,Operation=perfilamiento \
  --values 900 950 1000 --counts 20 20 20
# Repetir cada minuto durante 3-5 minutos: la alarma pasa a ALARM y notifica.
# Al dejar de publicar (treat_missing_data = notBreaching) vuelve a OK y notifica la recuperación.
```

## Validación sin desplegar

`make infra-validar` ejecuta `fmt`, `validate` y `terraform test` (plan completo con proveedores simulados, sin credenciales) de `bootstrap`, `platform` y `apps`.
