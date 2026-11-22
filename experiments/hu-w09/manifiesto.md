# HU-W09 · Manifiesto del experimento de carga

ASR ARQ-002 (cotización p95 ≤ 250 ms) y ARQ-003 (escala de 500 a 50.000 cotizaciones por minuto) · Ambiente `exp` · Plan: `docs/plan-experimento-hu-w09.md` (§b y §f)

> Este manifiesto se completa y se versiona **antes** de ejecutar. Lo que se mida después se compara con lo escrito aquí; si algo cambia, se registra como desviación en `informe.md` y este archivo no se reescribe.

## 1. Criterios (fijados antes de ejecutar)

| # | Criterio | Meta | Cómo se mide |
| --- | --- | --- | --- |
| C1 | ARQ-002: latencia de la cotización | p95 ≤ **250 ms** en cada etapa válida | p95 de `Solventa` / `OperationDuration` (`Environment=exp`, `Service=cotizacion`, `Operation=cotizacion`), minutos 2 a 5 de la etapa (decisión 12). Se informa también frente al corte de 700 ms |
| C2 | ARQ-003: capacidad | Tasa lograda ≥ **95 %** de la pedida en cada etapa válida | `logradaPorMinuto` / `pedidaPorMinuto` de la línea `HU-W09-ETAPA` de k6 |
| C3 | ARQ-003: errores técnicos | ≤ **1 %** por etapa válida (`error_rate_percent`, HU-W28) | `tasaError` de k6 (5xx, 4xx y errores de red, por separado) y `OperationErrors` / `SampleCount(OperationDuration)` de `cotizacion` |
| C4 | ARQ-003: autoescalado | La política agrega tareas antes de llegar al máximo, o el informe registra «al tope» | Tareas por servicio y tiempo de reacción (`informe.md`, §5) |

**Etapa válida** (decisión 15): k6 generó la tasa (`dropped` = 0). Si `dropped` > 0, se mira el p99 de k6 de la etapa:

- **p99 ≥ 1,6 s** (el presupuesto de `socio.cotizar`): los VUs estaban esperando al sistema hasta acercarse al tope de `maxVUs` (2 s × tasa). El cuello es el sistema; la etapa cuenta y se registra como incumplimiento.
- **p99 < 1,6 s:** faltó k6 (CPU o creación de VUs). La etapa queda fuera de la conclusión.

En la validación local de la 9.3, con un servidor a 1,9 s, k6 llegó a los 2.501 VUs y descartó entre el 1,3 % y el 2,8 % por etapa: así se ve el primer caso.

> **Confirmados** (2026-11-22, antes de ejecutar): C2, C3 y C4 quedan fijados tal como están arriba (propuestos en la 9.3).

## 2. Carga

| Etapa | Por minuto | Por segundo | Inicio (desde `t0`) | Duración | VUs preasignados / máximo |
| --- | --- | --- | --- | --- | --- |
| e1 | 500 | 8,3 | 0 min | 5 min | 7 / 17 |
| e2 | 2.000 | 33,3 | 5 min | 5 min | 25 / 67 |
| e3 | 5.000 | 83,3 | 10 min | 5 min | 63 / 167 |
| e4 | 10.000 | 166,7 | 15 min | 5 min | 125 / 334 |
| e5 | 25.000 | 416,7 | 20 min | 5 min | 313 / 834 |
| e6 | 50.000 | 833,3 | 25 min | 5 min | 625 / 1.667 |
| gw | 6.000 (`APIGW_TASA`) | 100 | 30 min 30 s | 5 min (`APIGW_DURACION`) | 75 / 200 |

- **Tramo `alb`** (e1–e6): `POST /cotizaciones` en el listener de `api-socios` del ALB interno (8083), con `X-Partner-Client-Id`, `X-Partner-Key-Id` y `X-Partner-Scopes = solventa/cotizar`, como los pondría API Gateway. No pasa por WAF, API Gateway, autorizador ni plan de uso.
- **Tramo `apigw`** (gw): WAF → API Gateway (plan `experimento`) → `api-socios`, con un token `client_credentials` por VU y la API key. Solo corre si llegan sus cinco variables.
- **Cuerpo:** `vida-hipotecario`, cobertura `ENFERMEDADES_GRAVES`, los datos declarados de C01, `Idempotency-Key` única por solicitud y `X-Correlation-Id = hu-w09-<etapa>-<la misma clave>`.
- **Clientes** (`CLIENTES`): C01–C10 y C13–C19, los de C01–C19 con identidad `VERIFICADO`. Se excluyen C11 (`RECHAZADO`) y C12 (`PENDIENTE`). La cotización por socios va sin `perfilId`: no llama a `adaptador-datos` ni al simulador, así que el comportamiento de las fuentes de cada cliente no interviene (`services/cotizacion/.../calcular_prima.py`, `_consultar`).
- **Socio:** alta interna con `alta.js` (`POST /admin/socios` por el ALB, plan `estandar`, permiso `cotizar`). Después, la preparación mueve su API key al plan `experimento` de API Gateway; la base de `api-socios` sigue diciendo `estandar` (decisión 2).

## 3. Configuración ejecutada (completar antes de ejecutar)

> Se completó el 2026-11-22, después de la corrida, con los logs de `Infra - Desplegar`, `E2E (N3)` e `Infra - Destruir` y con la línea `HU-W09-RECOGIDA` (`resultados/`). Los §1 y §2 no cambiaron.

| Dato | Valor |
| --- | --- |
| Commit de `main` (backend) | **Desplegado:** `0f7045e70e0fb2c6a96430b7e0b06099338c3564` (#91). Lo usaron los seis trabajos de `Infra - Desplegar`, y las imágenes llevan la etiqueta `0f7045e70e0f`. **Manifiesto, pruebas y destrucción:** `274771d52fd120f25bd68ed26aba57430209feee` (#93). Entre los dos solo hay documentación (#92 y #93). El código de los servicios y de `libs/`, y `carga.js`, son los mismos desde `f44268c` (#87): incluye W24 (#85 y #86) |
| Quién la corre | Santi-MISO |
| Ventana (UTC) y aviso al equipo | **Ambiente:** de 16:55:11, cuando empieza `Infra - Desplegar`, a 19:07:55, cuando termina `Infra - Destruir`, unas 2 h 13 min. La plataforma empieza a crearse a las 16:56:02 y la prueba rápida termina a las 17:25:56. **Pruebas:** de 17:59:04 (`preparar`) a 18:45:24 (`recoger`). **k6:** de 18:03:47 a 18:39:47. **Destrucción:** de 18:50:38 a 19:07:55. **Aviso:** al compañero de W29, antes de desplegar y al destruir. INT estaba abajo y W29 no corría (decisión 9) |
| `t0` (línea `HU-W09-INICIO`) | `2026-11-22T18:03:48.673Z` |
| ARN de la tarea k6 | `arn:aws:ecs:us-east-1:<cuenta>:task/solventa-exp/e6a6ae81e7a44869a824ea03509a9209` (STOPPED, `exitCode` 0, `EssentialContainerExited`) |
| Definición de la tarea k6 | `solventa-exp-carga:2`. El sha256 del script en `HU_W09_SCRIPT` es `ce034466c4b4bc8facc77efc8d9dff1be50f757eff8ea8920dc01a2169cf35cc` y **coincide** con el de `experiments/hu-w09/carga.js` del commit |
| Workflows | `Infra - Desplegar` (`ambiente = exp`, completo, `SERVICES = api-socios,cotizacion,catalogo,clientes`): planes de 342 y 123 recursos, `Apply complete` de los dos, y la prueba rápida con 34 OK y 0 fallas · `E2E (N3)`: preparar `121921951728`, lanzar `121922925722` y recoger · `Infra - Destruir` (confirmación `destruir exp`): 123 y 342 recursos destruidos, los ECR vaciados y la API key del socio eliminada. Los runs de desplegar, recoger y destruir no tienen número: sus archivos se descargaron renombrados. Extractos en `resultados/2026-11-22-{desplegar,destruir}.log` |
| Cuotas de Fargate (`preparar`) | On-Demand (`L-3032A538`) y Spot (`L-36FBB829`): **64 vCPU** cada una, aplicadas. La estimación era 4,5 vCPU al empezar y 9,5 con la cadena al máximo (k6: 2) |
| Socio | `SOC-000001`, dado de alta con `alta.js` en el plan `estandar`. **Plan efectivo de la API key: `experimento`**: `preparar` la movió con `delete_usage_plan_key` y `create_usage_plan_key`. En la base de `api-socios` sigue en `estandar` (decisión 2) |
| Cadena antes de lanzar (`preparar`) | `api-socios`, `cotizacion`, `catalogo` y `clientes`: ACTIVE, 2 deseadas y 2 corriendo, un despliegue cada uno y estables |
| Multizona | Dos zonas: RDS Multi-AZ y 2 NAT Gateway (ver la nota de `platform.tfvars`) |

### Imágenes

| Servicio | Digest |
| --- | --- |
| `api-socios` | `sha256:428cac90a5f88e9de8dae9d513e0f214056b671be502a7f5f6ad8897777555b8` |
| `cotizacion` | `sha256:d0369a19baee9e78728d5248d959e7bd63cf50d31ccf689e525ec3e562e6712b` |
| `catalogo` | `sha256:8ecac1e0ef3874eeba7caede9c6856b3e0ed3e4b5d6005ecd7c24396e0a2e57d` |
| `clientes` | `sha256:9693ad8dfc2ddad63513f36a76b9b92a878b9863845497deb565313f05fa1297` |
| `otel-collector` (sidecar de los cuatro) | `public.ecr.aws/aws-observability/aws-otel-collector:v0.43.3`, `sha256:8aa9ea5f67b8d318f7d6af24677e3c70f7098bc0631147cb5fa91addbe980b06` |
| k6 (`load_test_image`) | `grafana/k6:1.8.2@sha256:cbdf083a01e5245b2fdb3e4bb0cbcea5d52519b9aa66abd1f63b3f0249d18af9` (linux/amd64) |

Los digests salen de las definiciones de tarea que leyó `recoger`. Las imágenes de la cadena están en el ECR de `exp` (`<cuenta>.dkr.ecr.us-east-1.amazonaws.com/solventa-exp/<servicio>`). `Infra - Desplegar` publicó solo estos cuatro («Publicando api-socios cotizacion catalogo clientes con etiqueta 0f7045e70e0f»), esperó a 4 servicios estables, y la prueba rápida omitió a los demás por no tener imagen.

### Tarea k6

| Dato | Valor |
| --- | --- |
| CPU y memoria (`load_test_cpu`, `load_test_memory`) | 2048 / 4096: los valores por omisión de `infra/apps/variables.tf:395-408`, sin cambios en `envs/exp/apps.tfvars` |
| Comando | `entryPoint = ["sh", "-c"]` y `command = ["echo \"$HU_W09_SCRIPT\" \| base64 -d > /tmp/s.js && exec k6 run --log-format=raw -q /tmp/s.js"]`: el script viaja en base64 en la variable `HU_W09_SCRIPT` de la definición de tarea, sin S3 ni URL prefirmada (`infra/apps/carga.tf:68-69`) |
| Variables no secretas | `ALB_URL`, `SOCIO_SCOPES`, `CLIENTES`, `PRODUCTO_ID`, `APIGW_URL`, `APIGW_TOKEN_URL`, `APIGW_SCOPE`, `APIGW_TASA` (`ESCALA`, `DURACION_ETAPA` y `TIMEOUT` no se pasan: valen 1, 5m y 3s, `carga.js:68-70`; la corrida confirmó escala 1 y etapas de 300 s en `HU-W09-INICIO`) |
| Variables que no se registran | `SOCIO_CLIENT_ID`, `SOCIO_KEY_ID`, `APIGW_CLIENT_SECRET` y `APIGW_API_KEY` |

### `infra/envs/exp/platform.tfvars` (líneas relevantes)

| Variable | Valor |
| --- | --- |
| `db_instance_class` | `db.t4g.small` (`:21`) |
| `high_availability` | `true`: RDS Multi-AZ (standby en la zona B) y un NAT Gateway por zona, 2 en total (#84; `infra/envs/exp/platform.tfvars:13`) |
| `experiments_enabled` | `true`: rol de AWS FIS, bucket de informes y tarea de reconciliación de HU-W29, sin ejecutarse durante la corrida (#84; `infra/envs/exp/platform.tfvars:18`) |
| `cache_enabled` / `cache_node_type` | `true` / `cache.t3.micro` (`:32-33`) |
| `waf_rate_limit` (por IP en 5 min) | `50000` (`:38`) |
| `api_throttle` | `{ rate = 1000, burst = 2000 }` (`:36`) |
| `partner_tiers.experimento` | `{ rate = 500, burst = 1000, quota_limit = 5000000, quota_period = "DAY" }` (`:45`) |
| `alert_emails` | Una suscripción en `PendingConfirmation` (`preparar`). El correo no se copia (decisión 11) |

> **Multizona:** la medición se hace con EXP en dos zonas (RDS Multi-AZ, con escritura síncrona al standby, y 2 NAT Gateway, desde #84), mientras que el plan (`docs/plan-experimento-hu-w09.md` y la exploración D.12) suponía una sola zona. El informe lo declara al comparar con ARQ-002.

### `infra/envs/exp/apps.tfvars` (líneas relevantes)

| Variable | Valor |
| --- | --- |
| `task_cpu` / `task_memory` | `256` / `1024` (`:4-5`): `api-socios`, `catalogo` y `clientes` corren con 0,25 vCPU, que comparten con el sidecar `otel-collector` |
| `min_replicas` / `max_replicas` | `2` / `6` (`:6-7`) |
| `use_fargate_spot` | `false` (`:10`) |
| `cpu_target`, `scale_out_cooldown_seconds`, `scale_in_cooldown_seconds` | `60` (`:14`); `30` y `300`, los globales de `infra/apps/variables.tf:96-110` (EXP no los cambia ni por servicio) |
| `service_overrides` de los servicios de la cadena | Solo `cotizacion = { cpu = 512, memory = 1024 }` (`:20`). `api-socios`, `catalogo` y `clientes` toman los globales |
| `db_pool_size` | `5`, el valor por omisión (`infra/apps/variables.tf:128-130`) |
| `latency_budget_ms` | `cotizacion = 700`, `socio.cotizar = 1600` y `portal.cotizar = 1600` (`:47-51`) |
| `service_environment.cotizacion` | `SOLVENTA_FUENTE_TIMEOUT_MS = 600` y `SOLVENTA_DEPENDENCIA_TIMEOUT_MS = 300` (`:32-35`) |
| `latency_targets` (cotización) | `target_ms = 250` (`:64`) |
| `load_test_enabled` | `true` (`:81`) |
