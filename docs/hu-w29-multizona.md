# HU-W29: soporte multizona (implementación y experimento de ARQ-001)

Issue: [sjfuentes-uniandes/proyecto-final#71](https://github.com/sjfuentes-uniandes/proyecto-final/issues/71). ASR: [ARQ-001](https://github.com/sjfuentes-uniandes/proyecto-final/issues/28). Recorridos R1, R3 y R5 en `exp` (D4). Pantallas I04 e I05 del portal. Infraestructura: [infra/PLATAFORMA.md, HU-W29](../infra/PLATAFORMA.md#hu-w29-soporte-multizona). Experimento: [experiments/hu-w29/](../experiments/hu-w29/).

> **Estado.** La implementación está completa y validada sin AWS: N1, unitarias, contrato, Jasmine y Playwright. **R5 queda pendiente**: `adaptador-telemetria` todavía es el esqueleto de HU-S03 (solo `/health`) y `pagos-parametricos` no consume `parametricos-eventos`. La entrada firmada es HU-W21 y la creación del siniestro es HU-W22, ambas de capbmaster. El experimento en `exp` se ejecuta cuando las dos estén en main (sección 5).

## 1. Diseño en una página

- **Dos zonas reales en `exp`:** `high_availability = true` (RDS Multi-AZ y un NAT Gateway por zona), FARGATE sin Spot, `min_replicas = 2` y `availability_zone_rebalancing` en todos los servicios. En `int` hay tres puntos únicos de falla en la zona A (NAT instance, RDS Single-AZ y Valkey de un nodo), por eso el experimento no corre allí.
- **Falla controlada:** AWS FIS (`experiments/hu-w29/fis.json`) corta toda la red de las subredes privadas y de datos de la zona A durante 10 min (`aws:network:disrupt-connectivity`) y fuerza el failover de RDS (`aws:rds:reboot-db-instances`, `forceFailover = true`). La condición de parada es la alarma crítica `api-canales-5xx` de HU-W28.
- **Carga de fondo:** `carga.js` (k6) hace R1 (cotizar) y R3 (perfil → cotización → decisión → aceptación) por el borde real, con `Idempotency-Key` y reintento con la **misma** clave. Registra cada intento con su clave. R5 está escrito y desactivado (`R5=1`).
- **Reconciliación (AC2):** `extraer.sh` ejecuta una tarea ECS de solo lectura que extrae las filas de la ventana. `reconciliar.py` comprueba que cada operación confirmada (2xx) existe exactamente una vez, que cada aceptación tiene un solo cobro, que no queda nada en DLQ sin explicación y cuánto queda en el Outbox. También calcula el tiempo de redirección (AC3).
- **Pantallas:** I04 Continuidad (`/operacion/continuidad`) e I05 Conciliación (`/operacion/continuidad/conciliacion`), grupo `operacion`. `bff-web` solo lee CloudWatch (cronología, tráfico del ALB y alarmas) y el informe del bucket de `exp`.

## 2. Decisiones

| # | Decisión | Respaldo |
| --- | --- | --- |
| 1 | **Objetivo de recuperación** (fijado en el [manifiesto](../experiments/hu-w29/manifiesto.md) antes de ejecutar): redirección ≤ 10 min, 0 operaciones confirmadas perdidas o duplicadas, 0 cobros duplicados y 0 mensajes en DLQ sin explicación. Se reporta el consumo del presupuesto del SLO de la ficha (99,95 % mensual ≈ 21,6 min). La ficha no fija un tiempo de recuperación: el de 10 min se acordó con el responsable (coincide con "Compromiso ≤ 10 min" del mockup I04) | Ficha ARQ-001 (#28); mockup I04 |
| 2 | **Cronología por EventBridge**, no por las API de ECS y RDS desde `bff-web`: la regla de HU-W27 se completa con los eventos de RDS y FIS en el mismo grupo de logs. Las tareas por zona salen del último `ECS Task State Change` de cada tarea (Logs Insights) | `ECS ..> Observability` y `RDS ..> Observability` (`diagrama_despliegue_ecs_fargate.puml`) |
| 3 | **Bucket propio de `exp`** para el informe (`solventa-exp-experimentos-<cuenta>`); `bff-web` solo lee `hu-w29/*` | Ningún bucket de la plataforma se abre a los BFF (HU-S03) |
| 4 | **Reconciliación con una tarea ECS en la VPC** (como `db-bootstrap`), en solo lectura y con el usuario de cada servicio. Por las API no se probaría "existe una vez en su base" | Las bases son privadas (`network.tf`: subredes de datos sin ruta a Internet) |
| 5 | **Condición de parada `api-canales-5xx`** (5XX > 5 % en 3 de 5 min). La de disponibilidad evalúa por hora y no sirve de freno. Una parada por esta alarma es un resultado **no cumplido** | Alarmas de HU-W28 (`infra/apps/alarms.tf`) |
| 6 | Coordinación con Santi-MISO por este documento (sección 7) | — |
| — | Caché: sin cambios. La propuesta de réplica en B queda pendiente de aprobación ([PLATAFORMA.md](../infra/PLATAFORMA.md#caché-valkey-sin-cambios-propuesta-pendiente-de-aprobación)) | Cache-Aside descartable (`cache.tf`, `solventa_common/cache.py`) |

**Llamadas y colas nuevas:** ninguna entre servicios. `bff-web` lee "Métricas, trazas, registros y alertas" (`SeguimientoOperacion`, `modelo_despliegue.puml`), como HU-W27 y HU-W28. Cada pieza de infraestructura cita su flecha en [PLATAFORMA.md](../infra/PLATAFORMA.md#hu-w29-soporte-multizona) y en el código.

## 3. Contrato de `bff-web` (grupo `operacion`)

| Ruta | Fuente | Respuestas |
| --- | --- | --- |
| `GET /operacion/continuidad?horas=1..24` (6 por omisión) | Logs Insights y `FilterLogEvents` sobre `CONTINUIDAD_EVENTOS`; `RequestCount` del ALB por `AvailabilityZone` (últimos 5 min); alarmas abiertas y transiciones de HU-W28; informe (si existe) para los indicadores | 200 `ContinuidadRespuesta`; 403 sin el grupo; 422 ventana fuera de rango; 502 CloudWatch no disponible |
| `GET /operacion/continuidad/conciliacion` | `s3://<CONTINUIDAD_BUCKET>/hu-w29/informe.json` | 200 el informe **tal cual** (camelCase, versión 1); 404 sin informe (o fuera de `exp`); 502 S3 no disponible o formato distinto |

- **Estado por zona:** `SALUDABLE` (todas sus tareas esperadas saludables), `DEGRADADA`, `RETIRADA` (con tareas, pero el ALB no le entrega tráfico) y `NO_DISPONIBLE` (sin tareas saludables). El estado general es `DOS_ZONAS`, `UNA_ZONA` o `SIN_DATOS`.
- **Base:** `MULTI_AZ`, `FAILOVER_EN_CURSO` (RDS-EVENT-0013) o `STANDBY_PROMOVIDO` (RDS-EVENT-0015/0049). La zona de la primaria no sale en los eventos. La registra `distribucion.sh`.
- **Recorridos críticos:** `CONTINUIDAD_RECORRIDOS`, generado en `infra/apps/iam.tf` (R1, R3 y R5 con sus alarmas de HU-W28, solo las que existen). Un recorrido está `DEGRADADO` si alguna de sus alarmas está en ALARM.
- **Cronología:** inyección y fin (FIS), tareas retiradas y en ejecución por minuto y zona (ECS), failover (RDS) y alarmas abiertas y recuperadas, en orden de hora real, hasta 100 hitos.
- Esquemas en `contracts/openapi/bff-web.yaml`. Operaciones medidas: `consultar_continuidad` y `consultar_conciliacion` (`contracts/operaciones.md`).

## 4. Pruebas sin AWS

| Nivel | Comando | Qué cubre | Resultado (2026-11-11) |
| --- | --- | --- | --- |
| N1 Terraform | `TF_DATA_DIR=<tmp> make infra-validar` | `tests/exp` de platform (`multizona_hu_w29`): Multi-AZ, 2 NAT y cada subred por el de su zona, subredes en dos zonas, mínimo privilegio de FIS, bucket, cronología y reconciliación en solo lectura. `tests/exp` de apps (catálogo completo): mínimo ≥ 2, rebalanceo y FARGATE en los 8 críticos; solo `bff-web` lee `hu-w29/*`. `minimo_costo` y `todos_los_servicios` (`int`): sin recursos de FIS; cronología de ECS y RDS | platform 8/8 y exp 2/2; apps 10/10 y exp 2/2; Trivy sin críticos |
| Reconciliador | `uv run pytest experiments/hu-w29` | Operación faltante, duplicada, reintento con la misma clave, solicitud sin confirmar, cobro duplicado, aceptación sin cobro (pendiente o diferencia), DLQ con y sin explicación, R5 pendiente, consola de k6 (logfmt y JSON), redirección dentro y fuera del objetivo | 14 passed |
| bff-web unitarias | `make test-cov SERVICE=bff-web` | Dominio, adaptadores con Logs, CloudWatch y S3 simulados, caso de uso y rutas (`tests/unit/test_continuidad.py`) | 761 passed; módulos nuevos 99–100 % |
| bff-web contrato | `make test-contract SERVICE=bff-web` | OpenAPI publicado = app; esquemas de I04 e I05; **el informe que genera `reconciliar.py` valida contra el DTO de I05** | 51 passed |
| Web Jasmine | `npm run test:ci` (en `proyecto-final-2-frontend/web`) | I04 e I05 en es-CO y en-US, estados vacío y de error, exportación del JSON, axe-core, i18n (claves, pseudo) | 1070 SUCCESS |
| Web Playwright | `npx playwright test e2e/i04-i05-continuidad.spec.ts --project=chromium-escritorio --project=chromium-tableta` | Menú, ventana, error 502 con correlación, acceso por grupo, exportación y axe en la página completa (es-CO y en-US) | 12 passed |
| Carga | k6 contra un simulador local (sin AWS) | `carga.js` registra cada intento y reintenta el 503 con la misma clave; `reconciliar.py` lee la consola real de k6 | OK |

## 5. Experimento en `exp` (cuando HU-W21 y HU-W22 estén en main)

Cada paso con AWS requiere la confirmación del responsable. El costo estimado es ≈ 1,0 USD/h con el ambiente encendido, más ≈ 1,1 USD por ejecución de FIS (manifiesto, sección 5). Si algo falla a mitad: detenerse, recolectar lo que haya y consultar antes de destruir.

```bash
export ENV=exp
# 0. Antes de ejecutar: completar el manifiesto (commit, digests, tfvars) y activar R5 en carga.js.
make infra-desplegar ENV=exp                       # ~30 min; confirmar antes (costo por hora)
make infra-probar ENV=exp                          # N2: humo
experiments/hu-w29/distribucion.sh antes           # N2: cada crítico con tareas saludables en 2 zonas
E2E_CANALES_URL=... make test-e2e                  # R1 y R3 en verde (-k "hu_w03 or hu_w13_w15")
experiments/hu-w29/preparar-fis.sh                 # llena resultados/fis.json; imprime los comandos de FIS

# 1. Carga de 30 min; a los 5 min, iniciar FIS (comandos que imprime preparar-fis.sh)
k6 run --console-output=experiments/hu-w29/resultados/carga.log \
  -e CANALES=<api_urls.canales> -e TOKEN=<id token del back-office, grupo asesores> experiments/hu-w29/carga.js
experiments/hu-w29/distribucion.sh durante         # a los 5 min de la inyección
experiments/hu-w29/distribucion.sh despues         # al terminar la carga

# 2. Evidencia (DESDE = inicio de la carga − 1 min, HASTA = fin + 1 min, UTC)
DESDE=... HASTA=... experiments/hu-w29/recolectar.sh   # DLQ, alarmas, cronología y FIS
DESDE=... HASTA=... experiments/hu-w29/extraer.sh      # tarea ECS de solo lectura
#    revisar dlq.ndjson y escribir resultados/explicaciones.json {"<messageId>": "<motivo>"}
uv run python experiments/hu-w29/reconciliar.py --experimento "$(cat experiments/hu-w29/resultados/experimento.txt)" \
  --desde ... --hasta ... --inyeccion <startTime de InterrumpirZonaA en fis-experimento.json>
experiments/hu-w29/publicar.sh                     # informe en el bucket (lo muestra I05)

# 3. Capturas de I04 e I05, informe.md y destrucción
make infra-destruir ENV=exp                        # confirmar; luego verificar que no quedan solventa-exp-*
```

**Qué verificar:** en I04, la zona A pasa a `NO_DISPONIBLE` o `RETIRADA` y la B atiende el 100 % del tráfico; la base queda `STANDBY_PROMOVIDO`; la cronología muestra inyección, retiro, alarma, failover y recuperación. En I05, `SIN_DIFERENCIAS` y "Exportar informe" descarga el mismo `informe.json`. El resultado se lleva a `experiments/hu-w29/informe.md` (distribución, cronología, errores, reconciliación, costo y conclusión **cumplido / no cumplido** para ARQ-001).

**Riesgos conocidos:** (1) el VPC Link y el nodo del ALB en la zona A también se cortan: se esperan errores en los primeros segundos, que se miden y no se ocultan; (2) el rebalanceo por zona de ECS puede intentar poner tareas nuevas en la zona aislada: queda en la cronología; (3) si el nodo de Valkey está en A, sube la latencia de la cotización (Cache-Aside, sin pérdida de datos).

## 6. Pendiente

| Pendiente | Depende de | Qué falta |
| --- | --- | --- |
| R5 en la carga y en la reconciliación | HU-W21 y HU-W22 (capbmaster) en main | Encabezados de la firma, plantilla del evento (`R5_PLANTILLA`) y las consultas por `eventId` en `extraer.sh` (evento recibido en `adaptador_telemetria` y evaluación en `pagos_parametricos`); una verificación `eventos_parametricos` en `reconciliar.py` |
| Experimento en `exp` y `informe.md` | R5, y confirmación de despliegue | Sección 5 |
| Réplica de Valkey en la zona B | Aprobación | Propuesta en PLATAFORMA.md |
| Shellcheck de `experiments/hu-w29/*.sh` | CI | No está instalado en la Mac de desarrollo (se revisó con `bash -n`). El workflow **Infra - Validar** ahora lo corre, junto con las pruebas del reconciliador |

## 7. Coordinación

| Con | Tema | Acuerdo |
| --- | --- | --- |
| Santi-MISO (HU-W08 y HU-W09) | Rutas y menú de Operación | HU-W29 ocupa `/operacion/continuidad` y `/operacion/continuidad/conciliacion`, y activa "Continuidad" en el menú. **`/operacion/tablero` queda para el panel I01 de HU-W08**: hoy el ítem "Tablero" abre CloudWatch (`dashboardUrl`) y se cambia a `ruta: 'tablero'` en esa historia. Orden del menú: Tablero, Trazas, Alertas, Continuidad |
| Santi-MISO (HU-W09) | Carga | No había scripts k6 de HU-W09. `experiments/hu-w29/carga.js` sirve de base: tasas por variable (`R1_POR_MINUTO`, `R3_POR_MINUTO`), registro por intento con su clave y reintento idempotente |
| Santi-MISO (HU-W25 y escalado) | `exp` en dos zonas | Listo para usar: `exp` valida en N1 que cada servicio crítico tiene mínimo 2 réplicas repartidas entre A y B sin Spot. Si HU-W08 o HU-W25 cambian el escalado antes del experimento, el manifiesto registra el commit con que se corrió |
| capbmaster (HU-W21 y HU-W22) | R5 | Al terminar, avisar los encabezados de la firma HMAC, el cuerpo del evento y las tablas por `eventId` (sección 6) |

## 8. Archivos

- Infraestructura: `infra/envs/exp/platform.tfvars`, `infra/platform/{experiments,observability,outputs,variables}.tf`, `infra/apps/{iam,ecs}.tf`, `infra/apps/modules/ecs-service/main.tf` y sus pruebas (`tests/exp`, `platform.tftest.hcl`, `apps.tftest.hcl`).
- Experimento: `experiments/hu-w29/` (`fis.json`, `carga.js`, `reconciliar.py` y sus pruebas, `lib.sh`, `preparar-fis.sh`, `distribucion.sh`, `recolectar.sh`, `extraer.sh`, `publicar.sh`, `manifiesto.md`, `informe.md` y `resultados/`).
- `bff-web`: `domain/continuidad.py`, `application/consultar_continuidad.py`, `adapters/outbound/continuidad_aws.py`, rutas en `adapters/inbound/operacion.py` y `config.py`.
- Contratos: `contracts/openapi/bff-web.yaml` y `contracts/operaciones.md`.
- Portal (`proyecto-final-2-frontend/web`): `src/app/features/operacion/continuidad/`, `operacion.routes.ts`, `operacion-layout.component.ts`, `e2e/i04-i05-continuidad.spec.ts`, `eslint.config.mjs` y `package.json` (`lint:continuidad`).
