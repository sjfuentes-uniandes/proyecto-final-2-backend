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

| Dato | Valor |
| --- | --- |
| Commit de `main` (backend) | `<git rev-parse HEAD>` |
| Quién la corre | `<nombre>` |
| Ventana (UTC) y aviso al equipo | `<desde> – <hasta>` · `<enlace o fecha del aviso>` (INT abajo, sin coincidir con W29: decisión 9) |
| `t0` (línea `HU-W09-INICIO`) | `<t0>` |
| ARN de la tarea k6 | `<arn>` |
| Workflows | `Infra - Desplegar` `<run>` · `E2E (N3)` preparar `<run>`, lanzar `<run>`, recoger `<run>` · `Infra - Destruir` `<run>` |

### Imágenes

| Servicio | Digest |
| --- | --- |
| `api-socios` | `sha256:<…>` |
| `cotizacion` | `sha256:<…>` |
| `catalogo` | `sha256:<…>` |
| `clientes` | `sha256:<…>` |
| k6 (`load_test_image`) | `grafana/k6:1.8.2@sha256:cbdf083a01e5245b2fdb3e4bb0cbcea5d52519b9aa66abd1f63b3f0249d18af9` (linux/amd64) |

Si se despliegan más servicios que esos cuatro, se listan aquí con su digest.

### Tarea k6

| Dato | Valor |
| --- | --- |
| CPU y memoria (`load_test_cpu`, `load_test_memory`) | `<2048>` / `<4096>` |
| Comando | `entryPoint = ["sh", "-c"]` y `command = ["echo \"$HU_W09_SCRIPT\" \| base64 -d > /tmp/s.js && exec k6 run --log-format=raw -q /tmp/s.js"]`: el script viaja en base64 en la variable `HU_W09_SCRIPT` de la definición de tarea, sin S3 ni URL prefirmada (`infra/apps/carga.tf:68-69`) |
| Variables no secretas | `ALB_URL`, `SOCIO_SCOPES`, `CLIENTES`, `PRODUCTO_ID`, `APIGW_URL`, `APIGW_TOKEN_URL`, `APIGW_SCOPE`, `APIGW_TASA` (`ESCALA` y `DURACION_ETAPA` no se pasan: valen 1 y 5m) |
| Variables que no se registran | `SOCIO_CLIENT_ID`, `SOCIO_KEY_ID`, `APIGW_CLIENT_SECRET` y `APIGW_API_KEY` |

### `infra/envs/exp/platform.tfvars` (líneas relevantes)

| Variable | Valor |
| --- | --- |
| `db_instance_class` | `<db.t4g.small>` |
| `high_availability` | `true`: RDS Multi-AZ (standby en la zona B) y un NAT Gateway por zona, 2 en total (#84; `infra/envs/exp/platform.tfvars:13`) |
| `experiments_enabled` | `true`: rol de AWS FIS, bucket de informes y tarea de reconciliación de HU-W29, sin ejecutarse durante la corrida (#84; `infra/envs/exp/platform.tfvars:18`) |
| `cache_enabled` / `cache_node_type` | `<true>` / `<cache.t3.micro>` |
| `waf_rate_limit` (por IP en 5 min) | `<50000>` |
| `api_throttle` | `<{ rate = 1000, burst = 2000 }>` |
| `partner_tiers.experimento` | `<{ rate = 500, burst = 1000, quota_limit = 5000000, quota_period = "DAY" }>` |
| `alert_emails` | `<confirmada / PendingConfirmation>` (no se copia el correo: decisión 11) |

> **Multizona:** la medición se hace con EXP en dos zonas (RDS Multi-AZ, con escritura síncrona al standby, y 2 NAT Gateway, desde #84), mientras que el plan (`docs/plan-experimento-hu-w09.md` y la exploración D.12) suponía una sola zona. El informe lo declara al comparar con ARQ-002.

### `infra/envs/exp/apps.tfvars` (líneas relevantes)

| Variable | Valor |
| --- | --- |
| `task_cpu` / `task_memory` | `<256>` / `<1024>` |
| `min_replicas` / `max_replicas` | `<2>` / `<6>` |
| `use_fargate_spot` | `<false>` |
| `cpu_target`, `scale_out_cooldown_seconds`, `scale_in_cooldown_seconds` | `<60>`, `<30>`, `<300>` |
| `service_overrides` de los servicios de la cadena | `<cotizacion = { cpu = 512, memory = 1024 }>` |
| `db_pool_size` | `<5>` |
| `latency_budget_ms` | `<cotizacion = 700, socio.cotizar = 1600>` |
| `service_environment.cotizacion` | `<SOLVENTA_FUENTE_TIMEOUT_MS = 600, SOLVENTA_DEPENDENCIA_TIMEOUT_MS = 300>` |
| `latency_targets` (cotización) | `<target_ms = 250>` |
| `load_test_enabled` | `<true>` |
