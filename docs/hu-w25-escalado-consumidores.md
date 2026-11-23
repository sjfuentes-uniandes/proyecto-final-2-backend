# HU-W25 · Escalado de consumidores por atraso de colas

> Como responsable de operaciones quisiera que los consumidores de eventos aumenten según el atraso de las colas para recuperar el procesamiento después de un pico masivo.

Antes de W25, cada worker escalaba con una sola política, `backlog`: target tracking sobre la **suma** de los mensajes visibles de sus colas, con un objetivo global de 100 y enfriamientos fijos de 60 y 300 s. Con esa política, el objetivo significa cosas distintas con 1 tarea y con 3, y no reacciona a un mensaje que envejece. W25 cambia tres cosas, solo en `pagos-parametricos`:

- mide el atraso **por tarea**: `(visibles de sus dos colas) / RunningTaskCount`;
- agrega una política por **antigüedad** del mensaje más viejo, **solo hacia fuera**;
- **drena** al retirar una tarea sin perder ni duplicar mensajes y antes de `stopTimeout`.

`auditoria` y `avisos` siguen escalando por la suma de sus colas, como antes. La validación experimental es de HU-W26. Las réplicas y el drenaje HTTP vienen de HU-W07 ([hu-w07-replicas.md](hu-w07-replicas.md)), la política de CPU de HU-W08 ([hu-w08-autoescalado.md](hu-w08-autoescalado.md)) y el inbox y la idempotencia de `pagos-parametricos` de HU-W22 y HU-W24 ([hu-w24-particiones.md](hu-w24-particiones.md)). El diseño de referencia es el «Escalador de consumidores por atraso de particiones» de [`modelo_concurrencia.puml`](arquitectura/modelo_concurrencia.puml).

## 1. Las dos políticas (pasos 25.1 y 25.3)

Las dos están en [`infra/apps/autoscaling.tf`](../infra/apps/autoscaling.tf), sobre el mismo scalable target (`:25-32`).

| | `backlog` (`:51-123`) | `backlog_age` (`:130-152`) |
|---|---|---|
| Nombre | `<prefijo>-<servicio>-backlog` | `<prefijo>-<servicio>-antiguedad-<cola>`, **una por cola** |
| Servicios | Los workers con colas y `max > min` (`local.queue_scaled`, `:13`) | Los de `local.queue_scaled` que definen `queue_age_target` (`local.queue_age_policies`, `:18-22`) |
| Métrica | Por tarea: `(m0 + m1) / tareas` (`:69`). `m<i>` es `AWS/SQS ApproximateNumberOfMessagesVisible` de cada cola, `Average` (`:94-111`); `tareas` es `ECS/ContainerInsights RunningTaskCount`, `Average`, con `ClusterName` y `ServiceName` (`:72-93`) | `AWS/SQS ApproximateAgeOfOldestMessage`, `Maximum`, de su cola, sin metric math (`:141-149`) |
| Objetivo | `queue_backlog_target` (`:59`) | `queue_age_target`, en segundos (`:138`) |
| Enfriamientos | `queue_scale_out_cooldown` y `queue_scale_in_cooldown` (`:60-61`) | `queue_scale_out_cooldown` (`:139`) |
| Reduce | Sí | **No**: `disable_scale_in = true` (`:140`) |

- **Por qué `RunningTaskCount` con `Average`:** es un gauge de nivel de servicio, con un punto por minuto. Aunque haya más de un punto en el minuto (lo que rompió el `SampleCount` de `CPUUtilization` en HU-W09), `Average` sigue siendo un número de tareas, y es la misma estadística del numerador. `Maximum` contaría de más durante un despliegue (hasta el 200 %) y bajaría el atraso por tarea.
- **Sin el interruptor** (`queue_backlog_per_task = false`), la expresión es la de antes (`m0 + m1`, o `m0` con una cola), sin la métrica `tareas` y con el rótulo de antes (`:68-73`). Es el caso de `auditoria` y `avisos`.
- **Sin datos de `tareas`**, la alarma de `backlog` queda en `INSUFFICIENT_DATA` y no actúa en ningún sentido; la antigüedad sigue funcionando.
- **Una política por cola en lugar de `MAX(m0, m1)`:** escalar hacia fuera si cualquiera lo pide equivale al máximo de las antigüedades, y no depende de funciones de metric math que AWS no documenta para target tracking.

### Cómo conviven y por qué la reducción la decide solo `backlog`

Según la guía de Application Auto Scaling ([«Multiple scaling policies»](https://docs.aws.amazon.com/autoscaling/application/userguide/target-tracking-scaling-policy-overview.html#target-tracking-multiple-scaling-policies)):

> «It will scale out the scalable target if any of the target tracking policies are ready for scale out, but will scale in only if all of the target tracking policies (with the scale-in portion enabled) are ready to scale in.»

Y en la misma sección: «You can disable the scale-in portion of a target tracking scaling policy to use a different method for scale in than you use for scale out.»

Por eso:

- la subida la pide cualquiera de las tres políticas de `pagos-parametricos` (`backlog` o una de las dos `-antiguedad-<cola>`);
- la reducción solo la decide `backlog`, la única con la reducción habilitada. Con `disable_scale_in`, la antigüedad nunca baja tareas y no entra en la decisión de reducir: la reducción depende de una sola métrica (los visibles por tarea), y las dos políticas no oscilan entre sí (regla de la historia).

La regla está en la cabecera de `autoscaling.tf:4-10`.

### Comportamiento en INT

`pagos-parametricos` en INT: mínimo 1, máximo 3 (`infra/envs/int/apps.tfvars:6-7`), T = 10 y antigüedad de 180 s.

| Estado | Condición (alarmas de target tracking) | Qué hace App Auto Scaling |
|---|---|---|
| Estable | Visibles por tarea entre el 90 % de T y T, o sin datos | Nada |
| Sube por mensajes | Visibles por tarea > T durante 3 min | `desiredCount = min(max, ceil(actual × métrica / T))`; después, 60 s de enfriamiento de salida |
| Sube por antigüedad | Antigüedad de una cola > 180 s durante 3 min | Lo mismo, calculado sobre la antigüedad. Nunca reduce |
| Reduce | Visibles por tarea < 90 % de T durante 15 min | Baja hacia el valor calculado, nunca por debajo de `min`; después, 300 s de enfriamiento de entrada |
| Sin `RunningTaskCount` | La expresión no tiene datos | `backlog` no actúa; la antigüedad sigue. La precondición (§2) evita llegar aquí por un orden de aplicación equivocado |

## 2. Configuración por servicio y ambiente (pasos 25.1, 25.2 y 25.4)

### Variables

En [`infra/apps/variables.tf`](../infra/apps/variables.tf), separadas de las de CPU de HU-W08 (la prueba `politica_cpu_por_ambiente`, `apps.tftest.hcl:885`, sigue valiendo):

| Parámetro | Global | Por servicio (`service_overrides`, opcional, `:68-72`) | Validación |
|---|---|---|---|
| Interruptor por tarea | `queue_backlog_per_task`, `false` (`:148`) | `queue_backlog_per_task` (también `false`, para apagarlo con el global encendido) | — |
| Objetivo de `backlog` | `queue_backlog_target`, 100 (`:138`) | `queue_backlog_target` | > 0 |
| Antigüedad (s) | **No hay global**: sin valor, no hay política por antigüedad | `queue_age_target` | Entero ≥ 1 (`:88`); prohibido en `avisos` (`:97-98`) |
| Enfriamiento de salida (s) | `queue_scale_out_cooldown_seconds`, 60 (`:154`) | `queue_scale_out_cooldown` (vale también para la antigüedad) | Entero ≥ 0 |
| Enfriamiento de entrada (s) | `queue_scale_in_cooldown_seconds`, 300 (`:164`) | `queue_scale_in_cooldown` | Entero ≥ 0 |

`local.sizing` ([`infra/apps/main.tf:28-42`](../infra/apps/main.tf)) resuelve cada campo: el del servicio si lo tiene y, si no, el global, como en HU-W08. Con los valores por omisión, `backlog` queda en 100, 60 y 300 con la suma de las colas, igual que antes: el plan de un ambiente que no define estos campos no cambia. `avisos` no tiene consumidor, así que la validación rechaza `queue_age_target` en él: con una política por antigüedad, su cola sin consumir lo llevaría al máximo.

### Sin línea global

El interruptor se enciende **en la entrada de `pagos-parametricos` de `service_overrides`**, nunca con una línea global. Un global encendido pasaría también la `backlog` de `auditoria` y `avisos` a «por tarea» con T = 100: en EXP, con 2 tareas, `auditoria` escalaría con unos 200 visibles en vez de 100.

### Valores por ambiente

| Archivo y línea | Valor |
|---|---|
| `infra/envs/int/platform.tfvars:41` y `infra/envs/exp/platform.tfvars:35` | `container_insights = true` (paso 25.1, Plataforma) |
| `infra/envs/int/apps.tfvars:24` | `pagos-parametricos = { queue_backlog_target = 10, queue_backlog_per_task = true, queue_age_target = 180 }` |
| `infra/envs/int/apps.tfvars:55` | `pagos-parametricos = { SQS_CONCURRENCIA = "1" }` en `service_environment` |
| `infra/envs/exp/apps.tfvars:22` | `pagos-parametricos = { cpu = 512, memory = 1024, max = 10, queue_backlog_per_task = true, queue_age_target = 180 }` |

| | INT | EXP |
|---|---|---|
| Mínimo y máximo | 1 y 3 (los globales) | 2 (global) y 10 |
| Objetivo por tarea (T) | **10** | 100 (el global) |
| Antigüedad | 180 s, en cada cola | 180 s, en cada cola |
| Enfriamientos | 60 / 300 s (globales) | 60 / 300 s (globales) |
| `SQS_CONCURRENCIA` | **1** | 10 (el valor por omisión, `services/pagos-parametricos/src/pagos_parametricos/config.py:47`) |

**Por qué 180 s de antigüedad:** queda por encima de los 90–120 s que puede sostener un mensaje envenenado, y por debajo de la alarma de aviso de atraso de 300 s (`queue_age_alarm_seconds`, `infra/apps/variables.tf:386-389`).

**Por qué EXP queda en el global:** no hay datos para otro valor. Lo mide HU-W26.

### Por qué T = 10 y `SQS_CONCURRENCIA = 1` en INT

Para subir, target tracking pide 3 puntos de 1 min seguidos sobre el objetivo. La N2 necesita que la cola dure unos **240 s** sobre T: los 180 s de los 3 puntos, más un minuto de margen por la alineación de los puntos con los minutos y la demora de las métricas de SQS.

**Lo que cuesta un mensaje con `SQS_CONCURRENCIA = 1`** (estimación con el código, sin medir). Con un solo hueco, cada recepción pide un solo mensaje (`libs/solventa-common/src/solventa_common/inbox.py:570-579`). Cada mensaje es, en serie, un `ReceiveMessage`, la transacción en RDS `db.t3.micro` (el inbox, las coberturas de la zona, `eventos_evaluados`, el outbox y el commit) y un `DeleteMessage`. Las dos llamadas a SQS cruzan la NAT instance, porque INT no tiene endpoint de SQS, y la tarea tiene 0,25 vCPU.

| Tramo | Estimación |
|---|---|
| 2 llamadas a SQS por la NAT | 20–60 ms |
| Transacción (4 sentencias y el commit) | 5–15 ms |
| CPU con 0,25 vCPU | 10–30 ms |
| **Por mensaje** | **35–105 ms, es decir, c ≈ 10–28 mensajes/s por tarea** |

Con la ráfaga de 2.000 sola, la cola dura `D ≈ (2.000 − T) / c`, y `D ≥ 240 s` exige c ≤ ~8,3 mensajes/s con T = 10: el extremo bajo de la estimación. Por eso la N2 publica un complemento adaptativo (§4).

T = 10:
- es **lo bastante bajo** para que, hasta el final del vaciado, los visibles por tarea sigan sobre el umbral;
- es **lo bastante alto** para que el goteo de 1/s (con una capacidad de unos 30/s con 3 tareas) deje los visibles en 0–1, por debajo del 90 % de T (9), durante los 15 puntos de la reducción;
- **lleva la subida directo al máximo:** `ceil(1 × ~1.000 / 10)` se recorta a 3.

Ninguna otra variable existente baja el ritmo: la CPU de 256 ya es el mínimo de Fargate y `DB_POOL_SIZE` solo puede subir el pool. `SQS_CONCURRENCIA = 1` frena a `pagos-parametricos` en INT para todos. Las N3 de W21–W24 solo publican decenas de eventos.

### La precondición de Container Insights

`RunningTaskCount` solo existe con Container Insights encendido en el clúster:
- `infra/platform/compute.tf:5` pone el setting `containerInsights`;
- `infra/platform/outputs.tf:16` expone `container_insights` en el objeto `platform`. Es una salida aditiva del paso 25.1.

`infra/platform` **no se aplica al fusionar**: solo con **Infra - Desplegar**, alcance `completo` o `plataforma`. Para que un apply de `apps` con la plataforma vieja (por ejemplo, el de `cd-servicios` con un INT levantado antes de W25) no deje a `pagos-parametricos` sin escalar, `backlog` lleva un `lifecycle { precondition }` (`autoscaling.tf:114-122`):

```hcl
condition = !local.sizing[each.key].queue_backlog_per_task || try(local.p.container_insights, false)
```

Solo la comprueban los servicios que encienden el interruptor. Un `check` no serviría: en `plan` solo advierte, mientras que la precondición detiene el plan.

## 3. Drenaje de `pagos-parametricos` (paso 25.5)

Antes de W25, sus dos consumidores paraban **en serie** (15 s cada uno) y el CMD no tenía `--timeout-graceful-shutdown`. Sin una solicitud HTTP en curso, el peor caso ya era de 35,1 s, por encima de `stopTimeout = 30` ([`infra/apps/ecs.tf:165`](../infra/apps/ecs.tf)). W25 cambia dos cosas en [`services/pagos-parametricos/src/pagos_parametricos/main.py`](../services/pagos-parametricos/src/pagos_parametricos/main.py) y en su [`Dockerfile`](../services/pagos-parametricos/Dockerfile):

- `_en_fondo` (`main.py:78-110`) recibe varias tareas con **una sola `parada`** (`:96`) y **un solo límite para el conjunto** (`asyncio.wait(…, timeout=limite_s)`, `:106`). `_consumidores` le pasa los dos consumidores juntos (`:256-264`), y el relay va aparte (`:295`). `ConsumidorInbox` (`libs`) no cambió.
- El CMD lleva `--timeout-graceful-shutdown 4` (`Dockerfile:17`).

### El orden real del cierre

uvicorn 0.54.0 (`uv.lock:3230-3231`) espera a las solicitudes HTTP en curso **en serie y antes del lifespan** (`uvicorn/server.py:296-311`). Después, el lifespan cierra en orden LIFO lo que abrió `crear_app`.

| # | Momento | Qué pasa | Tope |
|---|---|---|---|
| 1 | SIGTERM | uvicorn cierra los sockets y deja de aceptar conexiones | 0,1 s (`uvicorn/server.py:293`) |
| 2 | Solicitudes HTTP | uvicorn espera las que están en curso y, al vencer el tope, las cancela | 4 s (`--timeout-graceful-shutdown`, `Dockerfile:17`) |
| 3 | Lifespan: los dos consumidores, **a la vez** | Abandonan la espera larga, liberan con `ChangeMessageVisibility 0` lo recibido sin empezar y dan `PLAZO_PARADA_S` = 10 s a lo que está en curso (`main.py:60`; `inbox.py:609`). Al vencer, cancelación, rollback y liberación | `PARADA_CONSUMIDOR_S` = 15 s para el conjunto (`main.py:61`) |
| 4 | Lifespan: relay del outbox | Publica lo último que escribieron los consumidores | `PARADA_RELAY_S` = 5 s (`main.py:55`) |
| 5 | Lifespan: pool | Cierra las conexiones, ya sin uso (`pool_base.close`, `main.py:335`) | 5 s (el `timeout` por omisión de `AsyncConnectionPool.close`, `psycopg_pool/pool_async.py:497`); en la práctica, milisegundos |
| 6 | Fin | uvicorn vuelve a lanzar SIGTERM y el proceso sale (143 en el contenedor) | — |

**Peor caso: 0,1 + 4 + 15 + 5 + 5 = 29,1 s**, por debajo de `stopTimeout = 30`. Sin solicitudes HTTP en curso (lo normal en un worker: `/health` tarda milisegundos), 25,1 s.

**Por qué 4 s y no 25 como en `cotizacion`:** como el HTTP va en serie antes del lifespan, con 25 s el peor caso era de 45,1 s (50,1 s con el pool), y ECS manda SIGKILL a los 30 s. `cotizacion` es HTTP y no tiene un lifespan largo detrás.

**Lo recibido sin borrar** queda en vuelo, sin perderse: si la tarea muere antes de liberarlo, reaparece al vencer la visibilidad, y el inbox evita el doble efecto (HU-W22 y HU-W24). Con la liberación, otra tarea lo toma enseguida, sin esperar la visibilidad.

La suma la comprueba `services/pagos-parametricos/tests/unit/test_hu_w25_dockerfile.py:101`, que lee cada término de donde sale: el CMD, `main.py`, la fuente de uvicorn, la de `psycopg_pool` y `stopTimeout` de `ecs.tf`.

## 4. N2: el escalado en INT (pasos 25.6 y 25.7)

[`tests/e2e/test_hu_w25_escalado.py`](../tests/e2e/test_hu_w25_escalado.py) tiene tres fases. Cada una corre solo si el `-k` contiene su propio nombre:
- `test_hu_w25_publicar`, `:1520`;
- `test_hu_w25_recoger`, `:1801`;
- `test_hu_w25_respaldo`, `:2116`.

Con el filtro vacío o con `-k hu_w25`, las tres se omiten. Solo corren con `ENV=int`, sobre el clúster `solventa-int` y el servicio `pagos-parametricos`. Se lanzan por **E2E (N3)** (`e2e.yml`), una ejecución por fase, porque el job corta a los 20 min y no pasa variables: las constantes van en el código (`:102-106` y `:119-148`). El recorrido, las guardas y los permisos están en [`infra/DESPLIEGUE.md`](../infra/DESPLIEGUE.md) §7, «Pruebas E2E (N3)». En local, el envoltorio [`scripts/plataforma/probar-escalado.sh`](../scripts/plataforma/probar-escalado.sh) (target `infra-probar-escalado` de `infra/plataforma.mk:63-64`) corre una fase con `ENV=int`, `SERVICE=pagos-parametricos`, `FASE=publicar|recoger|respaldo` y las credenciales de quien lo lanza.

**Guardas**, antes de publicar o forzar nada:
- las salidas de `infra/platform` son de `int`;
- hay credenciales;
- existen el scalable target (`min < max`), la política `-backlog` con `RunningTaskCount` en su configuración (de ella se lee T) y las dos `-antiguedad-<cola>`;
- en `publicar` y `respaldo`: `desiredCount == min` y `runningCount == desiredCount`;
- en `publicar`, además: `RunningTaskCount` con datos en los últimos 5 min y los visibles por tarea por debajo de T.

Si falta algo, la fase se omite con el motivo o, con `E2E_REQUERIDO=1` (lo pone `e2e.yml`), falla.

### `hu_w25_publicar`

| Línea del resumen | Qué muestra |
|---|---|
| `servicio al empezar:` | `desiredCount`, `runningCount` y `pendingCount` de `pagos-parametricos`, con su mínimo y su máximo. Sale también si una guarda falla. Debe ser 1 y 1 |
| `políticas:` | T, los enfriamientos y las métricas de `-backlog`, y las `-antiguedad-<cola>` |
| `colas al empezar:` y `DLQ al empezar:` | Los visibles por tarea frente a T, `RunningTaskCount` de los últimos 5 min y la profundidad de las DLQ |
| `corrida:` | El sufijo, el `eventoOrigen` (`E2E-W25-<sufijo>`), la fuente `E2E-W25` y la zona `W25SINPOL` |
| `centinela:` | Un evento que debe verse evaluado como `SIN_POLIZAS` en la auditoría en 180 s. Si no, la fase falla **sin publicar la ráfaga** |
| `ráfaga:` | Los 2.000 publicados (`PublishBatch` de 10) al inicio de un minuto, su hora y su duración |
| `ritmo por tarea:` | c, en mensajes/s, con su fuente (`NumberOfMessagesDeleted` del primer minuto, o la caída de `GetQueueAttributes` si el punto no llega en 180 s) y si es una cota inferior |
| `complemento:` | Los eventos publicados (0 si no hizo falta), el motivo y el cálculo con sus números |
| `total publicado:` | La ráfaga más el complemento, como mucho 10.000, sin contar el centinela ni el goteo |
| `subida:` | `desiredCount` de 1 a 3 y su hora, o «no se vio en el tope de la prueba»: el muestreo corta a los 15 min desde el inicio de la prueba, y `recoger` reconstruye la subida |
| `recoger desde:` | Cuándo lanzar `recoger` (enseguida) y a qué hora se espera la reducción |

### El complemento y su tope

`complemento` (`:361`) es una función pura con sus unitarios. Sus constantes:
- `RAFAGA` = 2.000;
- `VENTANA_S` = 240;
- `MARGEN` = 1,2;
- `TOPE_TOTAL` = 10.000, la ráfaga más el complemento;
- `LOTE` = 10.

No hay complemento si `(RAFAGA − T) / c ≥ VENTANA_S`, o si c = 0. Si hace falta, se calcula sobre lo que queda en la cola **en el momento de publicarlo** (`visibles_ahora`), y así la ventana de 240 s empieza cuando sale el complemento:

```
complemento = min(TOPE_TOTAL − RAFAGA, redondear_arriba_a_LOTE(c · VENTANA_S · MARGEN + T − visibles_ahora)), nunca negativo
```

| c (mensajes/s por tarea) | Complemento (con `visibles_ahora` = 0) | Total | ¿Alcanza para 240 s? |
|---|---|---|---|
| 8 | 0 | 2.000 | Sí, con la ráfaga sola |
| 15 | 4.330 | 6.330 | Sí |
| 28 (el extremo alto de la estimación) | 8.000 (tope) | 10.000 | Sí: 8.000 / 28 ≈ 286 s |
| > ~33 | 8.000 (tope) | 10.000 | No: menos de 240 s. Respaldo |

### `hu_w25_recoger`

Se lanza **en cuanto termina `publicar`**. Mientras `desiredCount` > mínimo, publica un **goteo de 1 evento por segundo**, aparte de la ráfaga y del complemento, con un tope de 10 min o 800 eventos (`GOTEO_TOPE_S` y `GOTEO_MAXIMO`, `:128-129`) y un `eventoOrigen` propio. Así la reducción ocurre con mensajes en curso. Si `pagos-parametricos` ya volvió al mínimo, solo recoge.

| Línea del resumen | Qué muestra |
|---|---|
| `goteo:` | Los eventos, la ventana y su `eventoOrigen`, o «sin goteo» |
| `corrida:` | Qué `publicar` reconstruyó (por el `idExterno`, que codifica la corrida, la clase y el total) y los goteos |
| `subida:` | La actividad de escalado: hora, de cuántas a cuántas tareas y qué política la pidió |
| `retiro:` | La reducción y la tarea retirada, con `stoppedReason`, `exitCode` y cuánto tardó |
| `liberados:` | Cuántos «inbox: mensaje liberado por la parada» hay en Logs Insights. **Se informa, no se exige:** con mensajes de milisegundos, que haya uno en curso al llegar el SIGTERM es cuestión de suerte. AC3 lo exigen el módulo y el SIGTERM de Linux (§3) |
| `tareas (H-3):` | Si `RunningTaskCount` coincide con las tareas de los eventos de ECS. Más de 1 de diferencia durante más de 2 min seguidos hace fallar la prueba |
| `reconciliación:` | Por `eventoId`: publicados, evaluados, faltantes, repetidos y el delta de las DLQ `parametricos-eventos-dlq` y `auditoria-dlq`. Deben ser 0, 0 y 0 |
| `todavía no volvió al mínimo:` | Si aparece, la hora para volver a lanzar `recoger`. La segunda vez no hay goteo |

Además, la tabla de la línea de tiempo por minuto: las tareas, `RunningTaskCount`, los visibles, los no visibles, la antigüedad, los enviados y borrados de las dos colas, las actividades de escalado y la historia de las alarmas de las tres políticas. El JSON completo queda en el log, con el prefijo `HU-W25-RECOGIDA`.

La reconciliación cubre el centinela, la ráfaga, el complemento y el goteo: exactamente un `EventoParametricoEvaluado` por cada evento publicado en los lotes `cadena/*.json` de la auditoría. No usa el portal.

### `hu_w25_respaldo`

**Solo si `recoger` no muestra ninguna subida**, ni con el complemento. Fuerza a `ALARM` la alarma alta de `-backlog` con un `StateReasonData` armado con la plantilla de la propia alarma, como HU-W08 (`datos_de_estado`, `:1015`). Si ya la forzó en las últimas 3 h, fuerza la de una `-antiguedad-<cola>` (`elegir_alarma`, `:912`). El valor pide el máximo de tareas (`valor_para_deseadas`, `:903`). El resumen lo marca como `DECLARADO: no es carga real`. Después se lanza otra vez `recoger`. Si se usa, los valores (T, la ráfaga y el tope) se revisan con el ritmo medido, para otro ciclo.

### Lo que declara la evidencia

`e2e.yml` no sube artefactos: no hay `e2e.xml`. La evidencia es el **resumen** y el **archivo de logs** (Download log archive) de cada ejecución. La redacción va en una PR de documentación aparte (`docs/evidencias/hu-w25/`) y declara:

- **`SQS_CONCURRENCIA = 1`** en `pagos-parametricos` de INT (`infra/envs/int/apps.tfvars:55`). No sale en el resumen: se toma de la definición de tarea del plan aprobado. Frena el ritmo a propósito, para que la cola dure;
- **la ráfaga, el complemento si lo hubo** (con el ritmo, su fuente, si es una cota y el cálculo), **y el total**;
- **el goteo**: cuántos eventos, en qué ventana y que va aparte de la ráfaga y del complemento;
- **el respaldo, si se usó**: qué alarma se forzó y que no es carga real;
- **los datos que quedan**: hasta unos 10.800 `EventoParametricoObservado` sintéticos y otros tantos `EventoParametricoEvaluado` en la auditoría de INT, bajo **Object Lock de 30 días** (`infra/envs/int/platform.tfvars:23-24`). Son de la zona `W25SINPOL`, sin coberturas (`SIN_POLIZAS`: ni siniestros ni pagos), con la fuente `E2E-W25` y un `eventoOrigen` propio por corrida. No llevan PII.

## 5. Limitaciones

- **La antigüedad no se ejercita con carga real.** La cola se vacía antes de que el mensaje más viejo pase de 180 s. AC1 por antigüedad lo demuestran N1 y, si se usa, el respaldo; la evidencia lo declara.
- **Sin AWS, no se verificó:**
  - **que App Auto Scaling acepte la expresión** `(m0 + m1) / tareas` con una métrica de `ECS/ContainerInsights` junto a las de `AWS/SQS`, ni la política por antigüedad. `validate` y los mocks no lo ven: lo verá el apply del ciclo de INT. Si lo rechaza, se destruye INT y se revisa con el log;
  - **un `ConcurrentUpdateException` entre las dos `backlog_age`.** El proveedor `aws` v6.64.0 no reintenta ese error en `PutScalingPolicy`. `depends_on` (`autoscaling.tf:151`) crea `backlog` antes que las políticas por antigüedad, pero las dos `backlog_age` de `pagos-parametricos` siguen en paralelo entre sí. Si una falla, basta con volver a lanzar **Infra - Desplegar** con alcance `aplicaciones`: la otra ya existe y el segundo apply la completa. Ninguna prueba con mocks ve ese orden;
  - el SIGTERM real de ECS en Fargate (se probó en Linux con uvicorn y dobles), los permisos del rol de E2E y los tiempos reales frente a los 20 min del job;
  - el costo real de Container Insights, que solo corre mientras el ambiente existe.
- **G-12, conexiones de RDS en EXP (lo resuelve HU-W26).** Cada tarea abre hasta `max(DB_POOL_SIZE, 2 × SQS_CONCURRENCIA + 3)` conexiones (`config.py:56-61`; `DB_POOL_SIZE` = 5 por omisión, `libs/solventa-common/src/solventa_common/settings.py:26`).
  - En INT, con `SQS_CONCURRENCIA = 1`, son 5 por tarea, y 15 con 3 tareas, en `db.t3.micro`.
  - En EXP, con el valor por omisión (10), son 23 por tarea, y 230 con 10 tareas, en `db.t4g.small` (`infra/envs/exp/platform.tfvars:21`). Según la fórmula por omisión de AWS para PostgreSQL, el máximo de esa instancia es de unas 225 (una estimación, sin medir y sin contar los demás servicios): no cabe.
  - W25 no sube el máximo de INT (3). La decisión de EXP (el máximo, la concurrencia o el tamaño de la base) es de HU-W26.

## 6. Pruebas

| Criterio | Nivel | Prueba | Qué cubre |
|---|---|---|---|
| AC1 (subir hasta el máximo, por mensajes por tarea o por antigüedad) | N1 | `infra/apps/tests/apps.tftest.hcl`: `por_tarea_encendido` (`:1171`), `por_tarea_por_servicio` (`:1243`) y `antiguedad_por_servicio` (`:1291`) | La expresión por tarea con `RunningTaskCount` `Average`, el clúster y el servicio; una política por antigüedad por cola con `Maximum`, su objetivo y el enfriamiento del servicio |
| AC1 | N1 (INT y EXP) | `infra/apps/tests/int/int.tftest.hcl`, `ambiente_int` (`:128`); `infra/apps/tests/exp/exp.tftest.hcl`, `escalado_hu_w25` (`:237`) | Con los tfvars de cada ambiente: T = 10 en INT y 100 en EXP, la antigüedad de 180 s en las dos colas y `SQS_CONCURRENCIA = "1"` en INT |
| AC1 | N2 | `tests/e2e/test_hu_w25_escalado.py`: `hu_w25_publicar` y, solo de respaldo, `hu_w25_respaldo` | La subida de 1 a 3 en INT por mensajes por tarea; por antigüedad, solo con el respaldo |
| AC2 (reducir sin bajar del mínimo) | N1 | `apps.tftest.hcl`: `politica_cola_por_servicio` (`:960`), `politica_cola_por_ambiente` (`:1007`) y `politica_cola_limites` (`:1050`); `antiguedad_por_servicio` (`:1291`) | El enfriamiento de entrada configurable; la antigüedad con `disable_scale_in = true`, así que la reducción la decide solo `backlog` |
| AC2 | N2 | `hu_w25_recoger` | La vuelta al mínimo, con el goteo en curso y la línea de tiempo |
| AC3 (drenar sin perder ni duplicar) | Unitaria | `services/pagos-parametricos/tests/unit/test_consumidores.py`: `test_los_dos_consumidores_reciben_la_parada_a_la_vez` (`:231`) y `test_el_cierre_de_los_dos_consumidores_no_pasa_de_parada_consumidor_s` (`:259`) | Ningún consumidor recibe después de la parada del otro; un solo límite para el conjunto |
| AC3 | Unitaria | `services/pagos-parametricos/tests/unit/test_hu_w25_dockerfile.py` (`:93`, `:97`, `:101` y `:116`) | El tope HTTP de 4 s, `stopTimeout` leído de `ecs.tf` y el apagado completo (29,1 s) por debajo de 30 |
| AC3 | Módulo (LocalStack y PostgreSQL) | `services/pagos-parametricos/tests/module/test_hu_w25_drenaje_colas.py:236` | La app real con su lifespan: lo en curso termina, lo recibido se libera, otra instancia lo procesa sin esperar la visibilidad y `eventos_evaluados` queda con las filas exactas |
| AC3 | Módulo (SIGTERM real, solo Linux) | `services/pagos-parametricos/tests/module/test_hu_w25_drenaje.py`: A (`:204`) y B (`:239`), con `apoyo_drenaje.py` | uvicorn con los argumentos del CMD, la app real y dobles sin red. A sale a los 2,6 s y B a los 10,2 s del SIGTERM. En Windows se omiten con el motivo |
| AC3 | N2 | `hu_w25_recoger` | La reconciliación por `eventoId` (faltantes, repetidos y DLQ) y los liberados, que se informan |
| Regla 1 (por tarea) | N1 | `infra/platform/tests/platform.tftest.hcl`: `container_insights_por_omision` (`:683`) y `container_insights_hu_w25` (`:698`); `infra/platform/tests/int/int.tftest.hcl:63` y `infra/platform/tests/exp/exp.tftest.hcl:222` | Container Insights apagado por omisión, encendido en INT y EXP, y la salida `container_insights` |
| Regla 1 | N1 | `apps.tftest.hcl`: `por_tarea_apagado` (`:1141`) y `por_tarea_global_con_excepcion` (`:1272`); `infra/apps/tests/apps_sin_container_insights.tftest.hcl` (`:100`, `:111`, `:123` y `:135`) | Apagado, la suma de siempre; el interruptor por servicio; la precondición detiene el plan sin Container Insights |
| Regla 2 (antigüedad solo de salida) | N1 | `antiguedad_por_servicio` (`:1291`), `antiguedad_sin_rango` (`:1383`), `antiguedad_en_avisos_invalida` (`:1355`) y `avisos_otros_campos_de_cola` (`:1368`) | `disable_scale_in`; sin política si `max = min`; nunca en `avisos` ni en un no-worker |
| Regla 3 (configurable por servicio y ambiente) | N1 | `apps.tftest.hcl`: `politica_cola_por_omision` (`:940`) a `politica_cola_enfriamiento_entrada_invalido` (`:1128`), y `politica_cpu_por_ambiente` (`:885`) | Los globales por omisión (100, 60 y 300), el override por servicio, por ambiente y sus validaciones; la de CPU sigue independiente |
| Regla 4 (SIGTERM antes de `stopTimeout`) | Unitaria y módulo | Las de AC3 | — |
| Regla 5 (idempotencia antes de subir el máximo) | — | Ya en `main` (HU-W22 y HU-W24) | INT no sube su máximo (3); EXP ya tenía 10 |
| N2: funciones puras | Unitarias, sin AWS | `test_hu_w25_unit_*`, en el mismo archivo (80) | `complemento` (sin complemento, el límite exacto, c = 0, el caso intermedio, el tope y el redondeo), `ritmo_por_tarea`, `reconciliar`, `coinciden_tareas`, `linea_de_tiempo`, `sobre_observado` (cumple `contracts/events/EventoParametricoObservado.v1.schema.json`) y las copias de W08 y W09 |
| N2: envoltorio | — | `scripts/plataforma/probar-escalado.sh` y `make infra-probar-escalado` | Una fase por ejecución, solo con `ENV=int` y `SERVICE=pagos-parametricos`; `shellcheck` limpio |

Comandos sin AWS: `terraform -chdir=infra/<raíz> test` (con `-test-directory=tests/int -var-file=../envs/int/<raíz>.tfvars` o su par de `exp`), `make test-cov`, `make test-module` y `make test-contract` con `SERVICE=pagos-parametricos`, y `uv run pytest tests/e2e/test_hu_w25_escalado.py -rs` sin `ENV` (corre los unitarios y omite las tres fases con su motivo).
