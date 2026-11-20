# HU-W09 · Informe del experimento de carga

| | |
| --- | --- |
| Fecha y ventana (UTC) | `<desde> – <hasta>` |
| `t0` de la curva | `<t0>` |
| Commit, digests y tfvars | `manifiesto.md`, §3 |
| Resultados en bruto | `resultados/` (copiados del log y del resumen de los jobs de `E2E (N3)`) |

## 1. Conclusión

| ASR | Resultado | Tramo que la sustenta | Etapas válidas | Por qué |
| --- | --- | --- | --- | --- |
| ARQ-002 (cotización p95 ≤ 250 ms) | **`<CUMPLIDO / NO CUMPLIDO>`** | `alb`; `OperationDuration` de `cotizacion` (decisión 12) | `<e1–e6>` | `<la etapa más alta con p95 ≤ 250 ms y lo que pasó después>` |
| ARQ-003 (500 → 50.000 por minuto) | **`<CUMPLIDO / NO CUMPLIDO>`** | `alb` (curva completa) | `<e1–e6>` | `<C2, C3 y C4 del manifiesto, etapa por etapa>` |
| Verificación de extremo a extremo | `<coherente / no coherente>` con el tramo `alb` | `apigw` (`<plan experimento / estandar>`) | gw | `<p95 y errores de gw frente a la etapa alb de tasa parecida>` |

`<Un párrafo: la conclusión en una frase por ASR, el cuello de botella (§6) y la limitación que más pesa (§7)>`

Desviaciones frente al manifiesto: `<ninguna / lista>`.

## 2. Configuración

La del manifiesto (§3), sin repetirla aquí. Además:

- **Clientes rotados:** `<la lista de la línea HU-W09-INICIO>`. Por omisión, C01–C10 y C13–C19 (identidad `VERIFICADO`); sin C11 ni C12. La cotización por socios va sin perfil y no consulta las fuentes, así que el comportamiento de las fuentes de cada cliente no interviene.
- **Tarea k6:** `<CPU>` / `<memoria>`. En local (9.3), con este script, k6 usó unos `<x>` MiB por VU, y la tarea quedó dimensionada con margen para el peor caso de `maxVUs` (2.501). `<En AWS: memoria y CPU máximas de la tarea k6, si se registraron>`.

## 3. Metodología

- **Dos tramos.**
  - `alb`: la curva completa, dentro de la VPC, contra el ALB interno. Mide desde el ALB: no incluye WAF, API Gateway, autorizador ni plan de uso.
  - `apigw`: una etapa corta por el borde real, después de la curva, por debajo del límite del WAF por IP.
- **Generador:** k6 `constant-arrival-rate`, un escenario por etapa con `startTime` escalonado. La tasa no depende de la latencia del sistema: si faltan VUs, k6 descarta iteraciones (`dropped_iterations`), y eso se revisa por etapa (decisión 15).
- **Cortes por etapa:**
  - En CloudWatch, `GetMetricData` a 60 s entre `t0 + 5·k + 1` y `t0 + 5·(k+1)` min: se descarta el primer minuto de cada etapa, que es la transición.
  - En k6, la etapa completa (submétricas `{scenario:eN,tipo:cotizacion}`).
- **Dos medidas de latencia:**
  - `OperationDuration` de `cotizacion`: la conclusión de ARQ-002 sale de esta.
  - k6: incluye `api-socios`, el ALB y la red, y en `gw` también el borde.
- **Errores:** k6 separa 4xx, 5xx y errores de red (timeout de 3 s). `cotizacion` da `OperationErrors`.

## 4. Resultados por etapa

### 4.1 Lo que vio k6

| Etapa | Pedida/min | Lograda/min | `dropped` | Solicitudes | Errores (4xx / 5xx / red) | Tasa de error | p95 (ms) | p99 (ms) | ¿Válida? |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| e1 | 500 | | | | | | | | |
| e2 | 2.000 | | | | | | | | |
| e3 | 5.000 | | | | | | | | |
| e4 | 10.000 | | | | | | | | |
| e5 | 25.000 | | | | | | | | |
| e6 | 50.000 | | | | | | | | |
| gw | 6.000 | | | | | | | | |

### 4.2 `cotizacion` (ARQ-002) y el ALB (minutos 2 a 5 de cada etapa)

| Etapa | `OperationDuration` p95 (ms) | p99 (ms) | ≤ 250 ms | ≤ 700 ms | `OperationErrors` / muestras | ALB `RequestCount` | ALB `TargetResponseTime` p95 / p99 (ms) | ALB 5XX (destino / ELB) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| e1 | | | | | | | | |
| e2 | | | | | | | | |
| e3 | | | | | | | | |
| e4 | | | | | | | | |
| e5 | | | | | | | | |
| e6 | | | | | | | | |

### 4.3 Tareas por servicio (`SampleCount` de `AWS/ECS CPUUtilization`, mínimo y máximo de la etapa)

| Etapa | `api-socios` | `cotizacion` | `catalogo` | `clientes` |
| --- | --- | --- | --- | --- |
| e1 | | | | |
| e2 | | | | |
| e3 | | | | |
| e4 | | | | |
| e5 | | | | |
| e6 | | | | |

`<Gráfica o enlace a resultados/: tareas por servicio en el tiempo, con las fronteras de las etapas>`

### 4.4 Recursos (promedio y máximo de la etapa)

| Etapa | CPU % (`api-socios` / `cotizacion` / `catalogo` / `clientes`) | Memoria % (los mismos) | RDS CPU % | RDS `DatabaseConnections` (máx.) | Valkey `EngineCPUUtilization` % |
| --- | --- | --- | --- | --- | --- |
| e1 | | | | | |
| e2 | | | | | |
| e3 | | | | | |
| e4 | | | | | |
| e5 | | | | | |
| e6 | | | | | |

### 4.5 Curvas

`<carga–p95/p99 (k6 y OperationDuration) y carga–errores, con la meta de 250 ms y el corte de 700 ms marcados>`

## 5. Reacción del autoescalado

Para cada actividad de escalado hacia fuera: la hora de `ALARM` de la alarma alta de la política, el `StartTime` de la actividad y el `RUNNING` de la tarea nueva (eventos de ECS), frente al inicio de la etapa. «Al tope» si `desiredCount` ya estaba en el máximo (6).

| Servicio | Etapa | Inicio de la etapa | `ALARM` | Actividad (`StartTime`) | Tarea `RUNNING` | Reacción (s, desde el inicio de la etapa) | Tareas antes → después | ¿Al tope? |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| | | | | | | | | |

## 6. Cuello de botella

`<El primer componente que se satura (CPU, memoria, conexiones de RDS, latencia propia en OperationDuration por servicio) y en qué etapa. Si es api-socios (una conexión a RDS por solicitud y un cliente HTTP por llamada), se acepta «no cumplido» y se propone una historia de rendimiento aparte (decisión 8)>`

## 7. Limitaciones

- **El tramo `alb` no incluye el borde:** WAF, API Gateway, el autorizador y el plan de uso quedan fuera. La curva completa no cabe por el borde desde una sola IP (WAF); solo `gw` lo cruza, a una tasa menor.
- **Plan `experimento` solo en API Gateway:** `api-socios` no tiene ese plan en su enum. La preparación mueve la API key del socio en API Gateway, y en la base de `api-socios` el plan sigue siendo `estandar` (decisión 2). `<Si falló: el tramo gw corrió en estandar, a APIGW_TASA = 2.400>`.
- **Máximo de 6 tareas por servicio** (`max_replicas` de EXP; decisión 7): limita lo que puede crecer cada servicio.
- **`db.t4g.small`:** con 6 tareas por servicio y los pools por tarea, el tope de conexiones queda en unas 156 de las ~200 de la instancia (decisión 7).
- **Un solo NAT** (`high_availability = false`) **y el WAF:** k6 sale por una IP. El WAF limita el tramo `gw` a 50.000 solicitudes cada 5 minutos por IP.
- **Una conexión a RDS por solicitud en `api-socios`** (y un cliente HTTP por llamada a `cotizacion`): es un candidato a cuello de botella ajeno a `cotizacion` (decisión 8).
- **k6 en una sola tarea:** si k6 no genera la tasa (`dropped` > 0 sin latencia del sistema por encima de 2 s), la etapa no cuenta (decisión 15).
- **Datos sintéticos:** 17 clientes y un producto. Cada solicitud usa una `Idempotency-Key` nueva, así que no hay repeticiones idempotentes en `SampleCount`.

## 8. Costo

| Concepto | Cantidad | Costo (USD) |
| --- | --- | --- |
| Fargate de los servicios (vCPU-hora y GB-hora) | | |
| Fargate de k6 | | |
| RDS, ElastiCache y NAT (por hora del ambiente) | | |
| ALB (LCU) | | |
| API Gateway, WAF y Cognito (tramo `gw`) | | |
| CloudWatch (métricas, logs y `GetMetricData`) | | |
| **Total de la ventana** | | |

`<Fuente: Cost Explorer filtrado por Environment=exp en la ventana, o una estimación con los precios de us-east-1 y lo que se midió>`
