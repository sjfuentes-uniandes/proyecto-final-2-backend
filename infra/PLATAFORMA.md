# Plataforma Solventa en AWS

Infraestructura para ejecutar en AWS las 13 historias de la columna **Ready** del backlog y, desde HU-S02, los servicios, datos, colas, secretos, almacenamiento y alarmas del Sprint 2 (cotización, contratación, emisión y consulta; sección [Sprint 2](#sprint-2-hu-s02)). Se basa en el modelo de despliegue (`docs/arquitectura/modelo_despliegue.puml`, `diagrama_despliegue_ecs_fargate.puml`), en la Entrega 8 de arquitectura (componentes, conectores y patrones).

| Raíz | Qué crea | Depende de |
| --- | --- | --- |
| `infra/platform/` | Red en 2 zonas, NAT instance (o NAT Gateway), KMS opcional, ECR, RDS PostgreSQL con base y usuario por servicio, clúster ECS y Service Connect, SNS/SQS/DLQ, S3 de auditoría con Object Lock, S3 de documentos de póliza, caché ElastiCache (Valkey, opcional), Cognito, ALB interno + VPC Link v2 + API Gateway REST + WAF, CloudFront para el portal y tópico de alertas | Nada |
| `infra/apps/` | Por cada servicio con imagen publicada: roles IAM, grupos de seguridad, tareas Fargate con colector ADOT, servicios ECS en capas, autoescalado, alarmas y tablero | Estado de `platform` e imágenes en ECR |

El catálogo de servicios vive en un solo lugar: `infra/platform/catalog.tf`. Allí se declaran nivel, puerto del listener del ALB, base de datos, uso de la caché, dependencias síncronas, eventos, colas y aliados. `apps` lo recibe por el output `platform`.

## Arquitectura desplegada

```
Portal web ─► CloudFront + S3 (SPA)
Portal web / App móvil ─HTTPS─► WAF ─► API Gateway "canales" ─┐  (JWT Cognito clientes/back-office)
Socio ──HTTPS (+mTLS opc.)──► WAF ─► API Gateway "socios" ────┤  (JWT client_credentials + API key + plan de uso)
                                                              ▼
                                          VPC Link v2 ─► ALB interno (8081/8082/8083)
                                                              ▼
 ┌──────────────────────── ECS Fargate · subredes privadas zonas A y B ────────────────────────┐
 │ bff-web   bff-movil   api-socios                      ◄─ capa de acceso                      │
 │ clientes  catalogo  cotizacion  polizas  dinero       ◄─ núcleo (REST interno, Service Connect)│
 │ consulta (proyección CQRS)                            ◄─ lecturas rápidas de pólizas          │
 │ adaptador-datos  adaptador-identidad                  ◄─ adaptadores aislados por aliado      │
 │ adaptador-pagos  adaptador-firma (firma y push)          (salida por NAT)                     │
 │ auditoria, avisos (workers SQS)   simulador-aliados (WireMock, integración)                  │
 └──────────────────────────────────────────────────────────────────────────────────────────────┘
        │ SQL/TLS        │ TLS            │ Outbox ─► SNS ─► SQS por consumidor (+DLQ)   │ HTTPS
        ▼                ▼                ▼                                              ▼
 RDS PostgreSQL     ElastiCache Valkey   auditoria, consentimientos-cotizacion,     S3 auditoría (Object Lock)
 (base por servicio) (catalogo,           dinero-aceptaciones, polizas-recaudos,     S3 documentos de póliza
                     cotizacion, consulta) consulta-proyeccion, avisos              (URL prefirmadas)
 Secrets Manager + KMS · CloudWatch (logs, EMF, alarmas → SNS alertas) · X-Ray (ADOT)
```

## Trazabilidad historia → infraestructura

| Historia | Recursos |
| --- | --- |
| **HU-W01** Generación de credenciales | Pool Cognito `socios` con servidor de recursos `solventa` y permisos (`partner_scopes`). Un app client `client_credentials` por socio, con secreto generado por Cognito que se muestra una vez. `api-socios` tiene permisos IAM para crear, actualizar o eliminar clientes OAuth y API keys, y para etiquetarlas al crearlas (`apigateway:PUT` sobre `/tags/` de `/apikeys/*`; sin él `CreateApiKey` responde AccessDenied y el alta falla con 503). Respuesta 401 para credenciales inválidas y 403 para permisos no asignados. Pool `backoffice` con grupo `administradores-socios` y MFA obligatorio. |
| **HU-W02** Cuota por cada socio | API REST `socios` con `api_key_required` y planes de uso (`partner_tiers`; en `int` también `prueba`: 1 solicitud/s, ráfaga 2 y 5 al día). El límite y la cuota se aplican **por API key**, así que el exceso de un socio no consume la capacidad de otro. 429 sin llegar a `api-socios`, con `X-Correlation-Id` y `Retry-After` por tipo: `partner_throttle_retry_after_seconds` (1 s, límite por segundo) y `partner_quota_retry_after_seconds` (3600 s, cuota agotada). Filtro de métricas `Solventa/Socios PartnerThrottled` por `ApiKeyId`. `api-socios` cambia el plan de una API key (`DELETE`/`POST /usageplans/{id}/keys`), lee los límites y el uso del plan (`GET /usageplans/{id}` y `/usage`) y los 429 del socio (`cloudwatch:GetMetricStatistics`). |
| **HU-W10** Datos autorizados | `adaptador-datos` con credenciales en Secrets Manager (`open-finance`, `datos-abiertos`), timeout configurable y salida por NAT. Base `cotizacion` para guardar datos mínimos y su procedencia. |
| **HU-W11** Cálculo perfil de riesgo | Servicios `cotizacion` y `catalogo`, cada uno con su base (versiones de reglas y perfiles). |
| **HU-W12** Consulta fuentes en paralelo | Un adaptador por grupo de aliados (*bulkhead*). `ALLY_*_TIMEOUT_MS` por dependencia. Trazas padre/hijas en X-Ray a través de ADOT. |
| **HU-W27** Correlación de métricas y trazas | API Gateway con X-Ray y logs de acceso JSON (`requestId`, `xrayTraceId`). Encabezado `X-Request-Id` inyectado en cada solicitud. Colector ADOT por tarea (OTLP → X-Ray + EMF). Tablero por ambiente. |
| **HU-W28** Generación de alertas | Tópico SNS `alertas` con correo (`alert_emails`). Cada meta tiene una alarma de advertencia (`warning_ratio`, 90 %) y una crítica: p95 del perfilamiento (400 ms) y de otras operaciones (`latency_targets`), tasa de error técnico por servicio (`OperationErrors / SampleCount(OperationDuration)`), disponibilidad por API y cuota de socio (`PartnerQuotaUsage`). Además: 5XX y p95 del API, destinos saludables, CPU/memoria, atraso de colas, DLQ (informativa) y RDS. Las alarmas no tienen acciones: dos reglas de EventBridge envían un correo en español al pasar a `ALARM` y al volver a `OK` desde `ALARM`. `bff-web` expone las alertas abiertas, los umbrales y el resumen del mes. Los umbrales se configuran en `apps.tfvars`. Guía: [docs/hu-w28-alertas.md](../docs/hu-w28-alertas.md). |
| **HU-W03** Cálculo de prima | `catalogo` (base propia: `productos` y `reglas_prima` versionadas) y `cotizacion` (tabla `cotizaciones`). Caché Valkey de HU-S02: `catalogo` la llena e invalida, `cotizacion` solo la lee. `bff-web` → `cotizacion` → `catalogo` y `clientes` (llamadas existentes). Meta p95 de la operación `cotizacion` en `latency_targets`. Sin recursos nuevos (sección [HU-W03](#hu-w03-cálculo-de-prima)). |
| **HU-W30** Verificación de identidad | Servicios `clientes` (base propia) y `adaptador-identidad` (secreto `kyc`, timeout propio para distinguir PENDIENTE de RECHAZADO). |
| **HU-W31** Verificación de consentimiento | `clientes` guarda los consentimientos y `cotizacion` los consulta antes de llamar al adaptador. La cola `consentimientos-cotizacion` invalida cachés cuando se revoca un consentimiento. Las decisiones quedan en la auditoría inmutable. |
| **HU-M06** Validación de cliente | Ruta `/movil/*` → `bff-movil` → `clientes` → `adaptador-identidad`. La regla WAF de tamaño de cuerpo queda en modo conteo para permitir la selfie. No se guarda biometría en AWS. **Registro previo a la cuenta:** `POST /movil/v1/registro/verificaciones` es la única ruta pública de `/movil` (sin authorizer; tope propio en el stage y límite por IP en el WAF). `clientes` devuelve un token firmado (30 min, ligado al correo) con el secreto `<prefijo>/registro/token`; la app lo manda en `ClientMetadata` del SignUp y el Lambda `pre-sign-up` del pool `clientes` rechaza el registro de `app-movil` sin él (el portal web no se ve afectado). El app client móvil solo puede escribir `email` y `name`. |
| **HU-M07** Ingreso biométrico | Cliente Cognito `app-movil` (PKCE, `enable_token_revocation`). La biometría del sistema operativo desbloquea el refresh token; revocarlo obliga a una autenticación completa. |
| **HU-M08** Registro de autorización | `clientes` + Outbox → SNS → SQS `auditoria` → worker `auditoria` → S3 con Object Lock (versionado, KMS, solo TLS). |
| **HU-M09** Revocación de consentimiento | Igual que HU-M08, más la cola `consentimientos-cotizacion`. |

**Sprint 2 (HU-S02):** HU-W03 a HU-W06 (cotización, caché y presupuesto de latencia), HU-W13 a HU-W17 (suscripción, aceptación, recaudo, firma y emisión), HU-W18, HU-M10 y HU-M11 (consulta y avisos). Detalle en la sección [Sprint 2](#sprint-2-hu-s02).

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
| Secrets Manager | 16 secretos (8 bases, 6 aliados, maestro de RDS y token de registro) | 30 días de prueba | ~6,4 USD/mes |
| Caché (Sprint 2) | ElastiCache Valkey `cache.t3.micro`, un nodo (`cache_enabled`) | 750 h/mes: 0 USD | ~10 USD/mes |
| Alarmas CloudWatch | ~101 con los 16 servicios (~146 métricas: metric math cobra cada métrica) | 10 métricas gratis | ~14 USD/mes |
| API Gateway, Cognito (usuarios), CloudFront, S3, SNS, SQS, ECR, X-Ray | Volumen de pruebas | Dentro de la capa gratuita | Centavos |

Estimado con los 16 servicios encendidos 24/7 (los 10 del Sprint 1 + 6 de HU-S02):
- **Con capa gratuita: ≈ 73 USD/mes**, casi todo Fargate Spot y WAF (con los 10 del Sprint 1 eran ≈ 45).
- **Pausado (`paused = true` en apps): ≈ 22 USD/mes.** Pausar lleva todas las tareas a 0 sin destruir nada; también se puede detener RDS hasta 7 días (`aws rds stop-db-instance`). La caché sigue encendida: `cache_enabled = false` en `platform.tfvars` la elimina.
- **Sin capa gratuita: ≈ 126 USD/mes encendido y ≈ 76 USD/mes pausado.**

El delta de HU-S02 está en [Costo del Sprint 2](#costo-del-delta-del-sprint-2).

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
- **Caché de catálogo (ElastiCache)**, **proyección CQRS** y **Saga** se crean desde HU-S02 (sección [Sprint 2](#sprint-2-hu-s02)). **Evidencias de siniestros** no se crea porque ninguna historia del Sprint 2 la usa.

## Sprint 2 (HU-S02)

Todo se declara en el catálogo (`local.catalog`, `local.queues`, `local.allies`), así que ECR, la base por servicio (tarea `db-bootstrap`), las colas con su DLQ, los secretos, IAM, ECS, el autoescalado y las alarmas salen de los mecanismos del Sprint 1, sin recursos manuales.

### Servicios

| Servicio | `tier` | Base | Caché | Publica | Consume | Llama a | Aliados | Escala por |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `polizas` | `nucleo` | Sí | — | Sí | `polizas-recaudos` | `adaptador-firma` | — | CPU |
| `dinero` | `nucleo` | Sí | — | Sí | `dinero-aceptaciones` | `adaptador-pagos` | — | CPU |
| `adaptador-pagos` | `adaptador` | — | — | — | — | `simulador-aliados` | `pagos` | CPU |
| `adaptador-firma` | `adaptador` | — | — | — | — | `simulador-aliados` | `firma`, `push` | CPU |
| `consulta` | `proyeccion` (nuevo) | Sí | Sí | — | `consulta-proyeccion` | — | — | CPU (responde lecturas síncronas con meta p95) |
| `avisos` | `worker` | Sí (Inbox y tokens de dispositivo) | — | — | `avisos` | `adaptador-firma` | — | Profundidad de su cola |

Llamadas nuevas de los BFF: `bff-web` → `polizas`, `consulta`; `bff-movil` → `consulta`, `avisos`. `catalogo` y `cotizacion` pasan a `cache = true`.

**Decisiones contra los diagramas (`docs/arquitectura`):**
- `dinero` → `adaptador-pagos`: `modelo_despliegue.puml` (`RecaudosIndemnizaciones --> ConexionPagos`).
- `polizas` → `adaptador-firma`: `diagrama_componentes_responsabilidades.puml` (`Polizas -> Adaptadores : firma y aviso`) y `modelo_concurrencia.puml` ("(4) Pólizas inicia la saga de recaudo, firma y emisión"). En `modelo_despliegue.puml` quien llama a `ConexionFirma` es la Saga (`EmisionAvisos`), que vive en `polizas`.
- Push por `adaptador-firma`: el adaptador "Firma electrónica y notificaciones" (`ConexionFirma`) atiende a ambos proveedores, por eso guarda los secretos `firma` y `push`, y `avisos` (la parte de avisos de `EmisionAvisos`) lo invoca. `avisos` no tiene credenciales de aliados.
- No se agregan llamadas que los diagramas muestran y la historia no pide (`bff-movil` → `polizas`, `api-socios` → `polizas`, `cotizacion` → `polizas`, `polizas` → `dinero`): la Saga usa eventos (`OfertaAceptada`, `Recaudo*`). Se agregan al catálogo cuando su historia las necesite.

**Capas de Service Connect:** con estas llamadas, `consulta` queda en la capa 0; los adaptadores nuevos en la 1; `polizas`, `dinero` y `avisos` en la 2. `bff-movil` sigue en la 3 y `bff-web` en la 5, así que **ningún servicio del Sprint 1 cambia de `module.layer_N` ni se recrea** (assert en `infra/apps/tests/apps.tftest.hcl`).

### Colas y eventos

Cada consumidor tiene su cola, filtrada por el atributo `eventType`, su DLQ (redrive tras 5 recepciones, retención de 14 días) y sus alarmas de atraso y DLQ. Ninguna cola se comparte.

| Cola | Consumidor | Filtro `eventType` | `visibility_timeout` |
| --- | --- | --- | --- |
| `auditoria` | `auditoria` | Ninguno: recibe todo | 60 s |
| `consentimientos-cotizacion` | `cotizacion` | Prefijo `Consentimiento` | 30 s |
| `dinero-aceptaciones` | `dinero` | `OfertaAceptada` | 60 s |
| `polizas-recaudos` | `polizas` | Prefijo `Recaudo` (`RecaudoConfirmado`; `RecaudoRechazado` para compensar la Saga) | 120 s (firma y documento) |
| `consulta-proyeccion` | `consulta` | Prefijos `Poliza` (`PolizaEmitida` y los cambios de la póliza) y `Siniestro` (sin productores todavía) | 30 s |
| `avisos` | `avisos` | `PolizaEmitida`, `RecaudoConfirmado` | 60 s |

Recorrido: `OfertaAceptada` → `dinero` cobra por `adaptador-pagos` → `RecaudoConfirmado` → `polizas` firma por `adaptador-firma`, guarda el documento y publica `PolizaEmitida` → `consulta` actualiza la vista y `avisos` notifica. Un mensaje que agota los reintentos queda en `<prefijo>-<cola>-dlq` y abre la alarma `<prefijo>-cola-<cola>-dlq`.

### Aliados y mínimo privilegio

| Recurso | Quién accede | Cómo se garantiza |
| --- | --- | --- |
| Secreto `pagos` | Solo `adaptador-pagos` | Rol de ejecución: `secretsmanager:GetSecretValue` solo sobre los secretos de sus `allies` |
| Secretos `firma` y `push` | Solo `adaptador-firma` | Igual |
| Bucket de documentos de póliza | Escribe solo `polizas` (`document_writers`: `s3:PutObject`); leen `polizas` y `consulta` (`document_readers`: `s3:GetObject`, `GetObjectVersion`) para firmar URL prefirmadas | Rol de tarea; nadie tiene `DeleteObject`. `polizas` entrega la URL al emitir (portal); `consulta`, en la billetera móvil (HU-M10), porque `bff-movil` no llama a `polizas`. Ningún BFF accede al bucket |
| Bases `polizas`, `dinero`, `consulta`, `avisos` | Cada servicio a la suya | Usuario y secreto propios (`db-bootstrap`), `REVOKE ALL ... FROM PUBLIC` |
| Caché | `catalogo`, `cotizacion`, `consulta` | Grupo de seguridad de la caché sin reglas en `platform`; `apps` agrega una regla 6379 por servicio con `cache = true` |

### Bucket de documentos de póliza

`<prefijo>-polizas-<cuenta>` (`infra/platform/storage.tf`): versionado (las versiones anteriores expiran a los `policy_documents_noncurrent_days`, 90), cifrado SSE-S3 (SSE-KMS con `use_customer_managed_key`), bloqueo de acceso público, `BucketOwnerEnforced` y una política que **niega** transporte sin TLS (`aws:SecureTransport = false`), TLS anterior a 1.2, `PutObject` sin el encabezado `x-amz-server-side-encryption` y `PutObject` con otro cifrado. La lectura es por URL prefirmada (válidas mientras lo sean las credenciales del rol que firma; usar vencimientos cortos). `policy_documents_force_destroy = true` en `int` permite destruir el ambiente con documentos.

### Caché ElastiCache

`infra/platform/cache.tf`: un nodo Valkey 8 `cache.t3.micro` (`cache_node_type`), sin réplica, en las subredes de base de datos (sin ruta a Internet), con TLS obligatorio (`transit_encryption_mode = "required"`) y cifrado en reposo. Sin auth token: no hay secreto que guardar en el estado y el acceso lo restringe el grupo de seguridad. `cache_enabled = false` no crea ningún recurso de ElastiCache y deja `CACHE_URL` vacía: los servicios deben funcionar contra RDS (Cache-Aside).

| Opción | Costo mensual | Por qué no |
| --- | --- | --- |
| **Nodo `cache.t3.micro`** (elegida) | 0 USD con capa gratuita (750 h); ~10 USD sin ella | — |
| Valkey Serverless | ≥ ~6 USD (100 MB mínimos) + ECPU, sin capa gratuita | Más cara con la capa gratuita y exige clientes con cluster mode |
| Nodo `cache.t4g.micro` | ~9 USD | Puede no entrar en la capa gratuita |

Si el nodo cae, la caché se pierde y los servicios leen de RDS; no hay datos que no estén en una base.

### Tiempos por ambiente (`apps.tfvars`)

- `ally_timeouts_ms`: `pagos` 5000, `firma` 5000 y `push` 2000 ms (además de los del Sprint 1). Llegan como `ALLY_<ALIADO>_TIMEOUT_MS`.
- `latency_budget_ms`: presupuesto por operación, en JSON en `LATENCY_BUDGET_MS` (solo `cotizacion`, HU-W05). En `int`: `{"cotizacion": 700}`, el corte del recorrido de DEF-03.
- `latency_targets` de `int`: `emision` pasa a `Service=polizas` y `consulta` a `Service=consulta`, sus dueños en el Sprint 2.

### Alarmas y tablero

| Alarma | Métrica | Umbral (`apps.tfvars`) | Severidad |
| --- | --- | --- | --- |
| `<prefijo>-proveedor-<aliado>-degradado` (uno por aliado: `open-finance`, `datos-abiertos`, `kyc`, `pagos`, `firma`, `push`) | `100 × Σ OperationErrors / Σ SampleCount(OperationDuration)` de las operaciones del aliado (`provider_operations`; ver `contracts/operaciones.md`). La usará HU-W06 | `provider_error_percent` (20 %) en 3 de 5 periodos de 60 s | Crítica |
| `<prefijo>-recaudos-pendientes` | `MAX(ApproximateAgeOfOldestMessage)` de `dinero-aceptaciones` y `polizas-recaudos` | `pending_collection_age_seconds` (900 s) | Crítica |
| `<prefijo>-avisos-push-fallos` | `Sum(OperationErrors)` de `avisos`/`aviso.push` en 5 min | `push_failure_count` (5) | Advertencia |
| `<prefijo>-cola-<cola>-atraso` y `-dlq` de las 4 colas nuevas | Mecanismo existente sobre `local.p.queues` | `queue_age_alarm_seconds`; DLQ > 0 | Advertencia; informativa |
| Tasa de error, CPU y memoria de los 6 servicios | Mecanismo existente sobre los servicios desplegados | Los del Sprint 1 | — |

Todas existen aunque el servicio no esté desplegado y, sin datos, quedan en OK. Llegan al correo por EventBridge y a la pantalla Operación › Alertas (`ALERTAS_CATALOGO`). El tablero `solventa-<ambiente>` suma dos filas: **Contratación** (p95 y errores de `polizas`, `dinero` y sus adaptadores, antigüedad y DLQ de la Saga y estado de las alarmas nuevas) y **Consulta** (p95 de `consulta` y `avisos` con la meta de consulta, colas de proyección y avisos, y aciertos, fallos y CPU de la caché cuando está habilitada).

### Costo del delta del Sprint 2

| Componente | Con capa gratuita | Sin capa gratuita |
| --- | --- | --- |
| 6 tareas FARGATE_SPOT de 0,25 vCPU / 1 GB | ~19 USD | ~19 USD |
| ElastiCache `cache.t3.micro` (Valkey) | 0 USD | ~10 USD |
| Secrets Manager: 4 bases y 3 aliados | ~2,8 USD | ~2,8 USD |
| Alarmas: 34 nuevas (~53 métricas) | ~5,3 USD | ~5,3 USD |
| S3 de pólizas, 4 colas + 4 DLQ, 6 repositorios ECR, logs | < 1 USD | < 1 USD |
| **Total del delta** | **≈ 28 USD/mes** | **≈ 38 USD/mes** |

Pausado (`paused = true`), el delta baja a ≈ 9 USD/mes (≈ 19 sin capa gratuita) porque solo quedan secretos, alarmas y la caché. Son estimaciones con precios de `us-east-1`; confirmarlas en la calculadora de AWS.

### Seguridad del Sprint 2

- **IAM:** el rol de ejecución de cada servicio solo lee los secretos de su base y de sus `allies`; el rol de tarea agrega `s3:PutObject` solo a `polizas` y `s3:GetObject` solo a `polizas` y `consulta`, ambos sobre `<bucket>/*`. Sin `DeleteObject`, `ListBucket` ni comodines de recurso nuevos. Con clave propia, solo esos servicios reciben `kms:Decrypt`/`GenerateDataKey` para los documentos.
- **Estado y variables:** los secretos de aliados son marcadores (`CAMBIAR`, con `ignore_changes`); la caché no tiene auth token; `apps.tfvars` y `platform.tfvars` no tienen secretos. Las contraseñas de las bases nuevas salen de `random_password`, como las del Sprint 1, y por eso **sí** quedan en el estado de `platform` (cifrado en S3).
- **Trivy:** sin hallazgos CRITICAL ni HIGH en los recursos nuevos (ver [Validación sin desplegar](#validación-sin-desplegar)).

## HU-W03: cálculo de prima

HU-W03 no agrega recursos, variables ni llamadas al catálogo de infraestructura: usa lo que HU-S02 ya declaró para ella (`catalogo` y `cotizacion` con `cache = true`; `cotizacion` → `catalogo`, `clientes`; `bff-web` → `cotizacion`; `api-socios` → `cotizacion` para HU-W04). Se despliega con los mismos targets y workflows (`make infra-microservicios SERVICES=catalogo,cotizacion,bff-web` o el workflow **Infra - Desplegar**; **CD servicios** sigue siendo un marcador). Diseño y pruebas: [docs/hu-w03-prima.md](../docs/hu-w03-prima.md).

| Pieza | Qué cambia | Diagrama que lo respalda |
| --- | --- | --- |
| `catalogo` | Tablas `productos` (estado y vigencia `[desde, hasta)`) y `reglas_prima` (versión, vigencia, definición y hash; inmutables). Semilla al arrancar: `vida-hipotecario` (COP) y `vida-hipotecario-2025` (vencido). API interna `GET /v1/productos/{id}`, `.../reglas-prima/vigente?fecha=` y `.../reglas-prima/{version}` | `modelo_informacion.puml` (Producto, cobertura y regla versionada), `modelo_dominio.puml` (Producto, Regla de precio) |
| `cotizacion` | Tabla `cotizaciones` (migración `003`, inmutable, idempotencia por actor). API interna `POST /v1/cotizaciones`, `GET /v1/cotizaciones/{id}` y `GET /v1/productos/{id}/condiciones` para los roles `ASESOR` y `SOCIO` | `modelo_componentes.puml` (3) "Cotización consulta productos y reglas vigentes"; `modelo_dominio.puml` (Cotización 1 o-- 0..1 Perfil) |
| Caché | `catalogo` escribe `catalogo:v1:*` al leer de su base (TTL 60 s; versiones cerradas 24 h) y borra el producto y sus reglas vigentes al arrancar. `cotizacion` lee esas claves y, si fallan, pide a `catalogo` por HTTP. Sin `CACHE_URL`, `cotizacion` usa una caché en memoria de 60 s | `CatalogoReglas --> CacheCatalogo : actualiza / invalida` y `PerfilCotizacion --> CacheCatalogo : lectura` (componentes, despliegue e información) |
| `bff-web` | `POST`/`GET /web/v1/cotizaciones` y `GET /web/v1/productos/{id}/condiciones` (grupo `asesores`), por la ruta `/web/{proxy+}` existente | `CanalesAPI -down-> Cotizacion : REST cotizar` |

**Colas y eventos:** ninguno nuevo. La cotización no publica eventos en esta historia (`OfertaAceptada` es de HU-W15).

**Variables:** ninguna nueva en Terraform. `catalogo` y `cotizacion` ya reciben `CACHE_URL`; los ajustes de HU-W03 tienen valores por omisión en el código y se pueden sobrescribir con `service_environment` si hiciera falta: `SOLVENTA_CACHE_TTL_S` y `SOLVENTA_CACHE_TIMEOUT_MS` (catalogo, 60 y 50), `SOLVENTA_CATALOGO_CACHE_TTL_S`, `SOLVENTA_CACHE_TIMEOUT_MS` y `SOLVENTA_COTIZACION_PRESUPUESTO_MS` (cotizacion, 60, 50 y 250). Los timeouts hacia `catalogo` y `clientes` son los de DEF-03 (`SOLVENTA_DEPENDENCIA_TIMEOUT_MS`, 300 en `int`).

**Seguridad:** sin permisos IAM nuevos (la caché se controla por red: la regla 6379 de `catalogo` y `cotizacion` ya existe). La cotización guarda la edad cumplida, no la fecha de nacimiento ni el documento; los logs llevan solo identificadores y `solventa_common` redacta además `fecha_nacimiento` y `suma_asegurada`. Un socio solo lee sus cotizaciones.

**Costo del delta:** 0 USD. No hay tareas, nodos, colas, secretos ni alarmas nuevos (la alarma de la meta `cotizacion` ya existía y ahora recibe datos). Las tablas nuevas caben en el RDS existente y las claves de caché ocupan unos KB del nodo `cache.t3.micro`.

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
| `POLIZAS_BUCKET`, `POLIZAS_BUCKET_SSE` | polizas, consulta (`document_writers`/`document_readers`) | Bucket de documentos de póliza y el cifrado que su política exige al escribir (`ServerSideEncryption` en `PutObject`: `AES256`, o `aws:kms` con clave propia). `polizas` escribe; `polizas` y `consulta` firman URL prefirmadas de lectura |
| `CACHE_URL` | Con `cache = true` en el catálogo: catalogo, cotizacion, consulta | `rediss://<nodo>:6379` (TLS obligatorio, sin contraseña: el acceso lo limita el grupo de seguridad). **Vacía** con `cache_enabled = false`: el servicio lee de RDS. HU-W03: `catalogo` escribe e invalida las claves `catalogo:v1:*`; `cotizacion` solo las lee (contrato en `contracts/README.md`) |
| `LATENCY_BUDGET_MS` | cotizacion (`latency_budget_readers`) | JSON operación → ms desde `latency_budget_ms` en `apps.tfvars`, por ejemplo `{"cotizacion":700}` (HU-W05) |
| `PARTNERS_USER_POOL_ID`, `PARTNERS_API_ID`, `PARTNERS_USAGE_PLANS`, `PARTNERS_TOKEN_URL` | api-socios | Alta de socios, cambio de plan y consumo. `PARTNERS_USAGE_PLANS` (JSON plan → id del plan de uso) define qué planes se pueden asignar en el ambiente |
| `JWT_ISSUERS` | Acceso | Emisores válidos para revalidar el token (defensa en profundidad) |
| `QUOTA_METRIC_INTERVAL_S` | api-socios | Cada cuánto publica `PartnerQuotaUsage` (`partner_quota_interval_seconds`, 300; 0 = no publica) |
| `ALERTAS_PREFIJO`, `ALERTAS_CATALOGO`, `ALERTAS_UMBRALES`, `ALERTAS_ADVERTENCIA_PCT`, `ALERTAS_ATENCION_MIN`, `ALERTAS_FALSO_POSITIVO_MIN` | bff-web | Pantalla Operación › Alertas: prefijo de las alarmas del ambiente, severidad/título/unidad/enlace por alarma, tabla de umbrales y parámetros del resumen del mes. Los genera `infra/apps/alarms.tf` |
| `OTEL_*` | Todos | Exportación OTLP a `localhost:4317` (colector ADOT) |

**Encabezados que agrega API Gateway** a partir del token validado (sobrescriben lo que envíe el cliente): `X-Authenticated-Sub` (canales), `X-Authenticated-Issuer` y `X-Authenticated-Groups` (portal web, grupos de Cognito), `X-Partner-Client-Id`, `X-Partner-Scopes` y `X-Partner-Key-Id` (socios). `api-socios` responde 403 cuando el permiso requerido no está en `X-Partner-Scopes` (HU-W01 AC4).

**Eventos:** el relay del Outbox publica en SNS con el atributo de mensaje `eventType` (`String`), por ejemplo `ConsentimientoOtorgado`, `ConsentimientoRevocado` o `IdentidadVerificada`. El cuerpo es el sobre del evento con `eventId`, que los consumidores usan para la idempotencia. La cola `auditoria` recibe todos los eventos; `consentimientos-cotizacion` recibe solo los `Consentimiento*`. Las colas del Sprint 2 están en [Colas y eventos](#colas-y-eventos). Un evento sin `eventType` solo llega a `auditoria`.

**Métricas** (usadas por alarmas y tablero): espacio de nombres `Solventa`, dimensiones `Environment`, `Service` y `Operation`. Los servicios publican con OpenTelemetry:
- `OperationDuration`: histograma en ms. `OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION` ya lo configura como exponencial, lo que permite calcular p95.
- `OperationErrors`: contador.

El perfilamiento debe usar `Service=cotizacion` y `Operation=perfilamiento`. Las operaciones de `latency_targets` (en `int`: `cotizacion`, `emision` y `consulta` de `cotizacion`) deben publicarse con esos nombres para que sus alarmas evalúen datos; mientras no existan, quedan en OK.

**Cuota de socio (HU-W28):** `api-socios` publica cada `QUOTA_METRIC_INTERVAL_S` el porcentaje de la cuota consumido por cada socio activo en `Solventa/Socios PartnerQuotaUsage` (unidad `Percent`, dimensiones `Environment` y `Partner` = id del socio), con `cloudwatch:PutMetricData` limitado a ese espacio de nombres. La alarma usa Metrics Insights (`MAX` entre socios); `bff-web` identifica al socio con el mayor consumo.

**Logs:** JSON por línea con `service`, `operation`, `result`, `duration_ms` y `correlation_id`. Nunca incluir secretos, tokens, biometría ni payloads financieros completos (HU-W27 AC4).

**Instrumentación (HU-W27):** `solventa_common.telemetry.instrumentar(app, settings)` en el `main.py` de cada servicio aplica lo anterior:
- Correlación: `X-Correlation-Id` válido (hasta 128 caracteres de `[A-Za-z0-9._:-]`), si no `X-Request-Id`, si no `uuid4`. Se devuelve en la respuesta y queda en `solventa_common.correlation.obtener()`. Para llamar a otro servicio: `correlation.cliente_async()` o `correlation.cliente()` (propagan el ID y la traza).
- Métricas y log por solicitud con `Operation = "<MÉTODO> <plantilla de ruta>"`, y por operación de negocio con `with operacion("perfilamiento"):` o `@operacion("perfilamiento")` (resultado `ok`, `error`, `timeout` o `rechazado`). El healthcheck local de ECS no se mide ni se traza.
- Logs: `extra={...}` pasa por `solventa_common.logging.sanitizar` (claves sensibles redactadas en cualquier nivel, textos truncados).
- Trazas: el colector indexa `correlation_id` y `operation` como anotaciones de X-Ray: `annotation.correlation_id = "prueba-123"`.
- `OTEL_SDK_DISABLED=true` o sin `OTEL_EXPORTER_OTLP_ENDPOINT`: todo funciona sin exportar.

**Trazas en el portal:** `bff-web` expone `GET /web/operacion/trazas?correlation_id=&recorrido=&duracion_min_ms=&horas=` solo para el grupo `operacion` del back-office; consulta X-Ray (`xray:GetTraceSummaries`, `xray:BatchGetTraces`, solo en su rol de tarea). `CORS_ORIGINS` (JSON) lleva la URL de CloudFront y `cors_extra_origins`.

**Alertas en el portal (HU-W28):** `GET /web/operacion/alertas` (grupo `operacion`) devuelve las alertas abiertas, los umbrales configurados y el resumen de los últimos 30 días. `bff-web` tiene `cloudwatch:DescribeAlarms`, `DescribeAlarmHistory` y `GetMetricData` (solo lectura, `Resource = "*"` porque el historial sin nombre de alarma y Metrics Insights no admiten ARN) y filtra por `ALERTAS_PREFIJO`.

## Probar la alerta sin carga (HU-W28 AC4)

```bash
make infra-probar-alerta ENV=int                    # NIVEL=critica: 900-1000 ms
NIVEL=advertencia make infra-probar-alerta ENV=int  # entre la advertencia y la meta
```

El script publica `OperationDuration` sintético con las dimensiones de la alarma (`Environment`, `Service=cotizacion`, `Operation=perfilamiento`) cada minuto, espera `ALARM`, deja de publicar y espera `OK` (`treat_missing_data = notBreaching`). Imprime los tiempos y sale con 0 o 1. Las alarmas del perfilamiento existen aunque `cotizacion` no esté desplegado. Detalle y correos esperados: [DESPLIEGUE.md](DESPLIEGUE.md#52-probar-las-alertas-hu-w28).

## Validación sin desplegar

`make infra-validar` ejecuta `fmt`, `validate` y `terraform test` (plan completo con proveedores simulados, sin credenciales) de `bootstrap`, `platform` y `apps`, y luego Trivy (`scripts/plataforma/seguridad.sh`): informa los hallazgos HIGH y falla con cualquier CRITICAL que no esté justificado en `infra/.trivyignore.yaml`. Usa `trivy` del PATH o la imagen `aquasec/trivy` con Docker; `TRIVY=0` lo omite.

Excepciones vigentes (todas del Sprint 1, justificadas en el archivo): listeners HTTP del ALB interno (TLS termina en API Gateway) y salida a `0.0.0.0/0` desde subredes privadas por la NAT. Hallazgos HIGH que se aceptan por costo: buckets del portal y del estado sin clave KMS propia (`use_customer_managed_key = false`) y CloudFront del portal sin WAF propio (la API sí lo tiene). Trivy no evalúa los recursos de `apps` que dependen del estado remoto (`for_each` sobre `local.deployed`); su revisión es la de [Seguridad](#seguridad-del-sprint-2).
