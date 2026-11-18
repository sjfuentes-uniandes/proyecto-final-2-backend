# HU-W08 · Autoescalado de la cotización

> Como responsable de producto quisiera que Solventa aumente automáticamente las réplicas ante un pico de solicitudes para conservar el tiempo de respuesta de la cotización.

W08 no agrega la política de escalado: `aws_appautoscaling_policy.cpu` ya existía (`infra/apps/autoscaling.tf`). La historia la parametriza por ambiente y por servicio, sube el máximo de `cotizacion` en INT, deja que `bff-web` lea la capacidad para el panel del tablero y demuestra el escalado en INT sin generar carga. Las réplicas sin estado local y el drenaje al retirar una tarea vienen de HU-W07 (`docs/hu-w07-replicas.md`).

## 1. Parametrización (paso 8.1)

| Parámetro | Global (`infra/apps/variables.tf`) | Por servicio (`service_overrides`, opcional) |
|---|---|---|
| Objetivo de CPU (%) | `cpu_target`, 60 (de 1 a 100) | `cpu_target` |
| Enfriamiento hacia fuera (s) | `scale_out_cooldown_seconds`, 30 | `scale_out_cooldown` |
| Enfriamiento hacia dentro (s) | `scale_in_cooldown_seconds`, 300 | `scale_in_cooldown` |

- `local.sizing` (`infra/apps/main.tf`) resuelve cada campo: el valor del servicio si lo tiene y, si no, el global. `min`, `max` y `paused` no cambian.
- La política `cpu` toma el objetivo y los enfriamientos de `local.sizing`. Con los valores por omisión, queda igual que antes (60/30/300) para todos los servicios: el plan de INT no cambia por este paso.
- Cambiar estos parámetros actualiza la política en el sitio, sin recrear el servicio ECS.
- La política de los workers (`backlog`) no cambia.

## 2. INT (paso 8.2)

`cotizacion` queda con `min = 2` (de HU-W07) y `max = 4` (`infra/envs/int/apps.tfvars`), con el objetivo global (60 %): no se inventó otro valor, y HU-W09 puede ajustarlo con datos. En el plan de INT solo cambia `max_capacity` del scalable target de `cotizacion`, de 3 a 4. El servicio ECS no cambia: `desired_count` lo maneja el autoescalado.

## 3. Permisos de `bff-web` (paso 8.3)

- Solo `bff-web` (`capacity_readers`, `infra/apps/iam.tf`) recibe `application-autoscaling:DescribeScalableTargets` y `DescribeScalingActivities`, de solo lectura. La referencia de autorización de AWS no les da tipo de recurso, así que van con `Resource = "*"`.
- `bff-web` recibe `CAPACIDAD_CLUSTER` y `CAPACIDAD_SERVICIO` (`infra/apps/ecs.tf`), todo o nada: solo si `cotizacion` está desplegado.

## 4. Ruta `GET /operacion/capacidad` de `bff-web` (paso 8.4)

- Solo para el grupo `operacion`. Devuelve los últimos 60 minutos completos, en snake_case:
  - cotizaciones por minuto (`SampleCount` de `OperationDuration` de `cotizacion`);
  - tareas activas (`SampleCount` de `CPUUtilization` de ECS; null sin datos);
  - el máximo del scalable target;
  - el último escalado de la ventana, con su duración.
- Si falta la configuración o falla AWS, responde 503 `SERVICIO_NO_DISPONIBLE` sin `detalles` (`contracts/codigos-error.md`).
- El código está en `domain/capacidad.py`, `application/consultar_capacidad.py`, `adapters/outbound/capacidad.py` y `adapters/inbound/operacion.py` (`router_capacidad`). Las rutas son relativas a `services/bff-web/src/bff_web/`.
- El contrato está en `contracts/openapi/bff-web.yaml`, con un cambio solo aditivo.

## 5. N2: el escalado en INT (paso 8.5)

`tests/e2e/test_hu_w08_escalado.py` lleva a `ALARM` la alarma alta de la política `solventa-int-cotizacion-cpu` y observa a `cotizacion`:

- `desiredCount` sube sin pasar del máximo (AC1);
- vuelve al mínimo sin bajar de él (AC2);
- la tarea retirada sale con su `stoppedReason` y su `exitCode` (AC3).

Toma el nombre de las alarmas de la respuesta de `DescribeScalingPolicies` (`politica_de`, con unitarios en el mismo archivo). Nunca cambia el scalable target ni la política. Se lanza por `e2e.yml` con `-k hu_w08`. Las guardas, las variables y cuándo es seguro lanzarla están en `infra/DESPLIEGUE.md` §7 («Pruebas E2E (N3)»). Reemplaza al `scripts/plataforma/probar-escalado.sh` de la historia, porque ese script necesitaba credenciales locales de AWS (plan de W08, §b, decisión 2).

**Riesgo:** una política de *target tracking* calcula la capacidad con el valor real de la métrica, y con poca carga la CPU está por debajo del objetivo. Forzar la alarma alta podría no subir `desiredCount`. La N2 lo demuestra o lo descarta: si no sube en la espera, falla con lo observado (estado e historial de la alarma, actividades de escalado), sin intentar otra vía. La bajada puede tardar más que la espera, por el enfriamiento de 300 s y la evaluación de la alarma baja. En ese caso la prueba no falla: el autoescalado devuelve el servicio al mínimo por su cuenta.

## 6. Pruebas

| Nivel | Prueba | Qué cubre |
|---|---|---|
| N1 | `infra/apps/tests/apps.tftest.hcl`: `politica_cpu_por_omision`, `_por_servicio`, `_por_ambiente` y `_invalida` | 60/30/300 por omisión para todos; un override solo cambia su servicio; los globales por ambiente; validaciones (§1) |
| N1 | El mismo archivo: `capacidad_bff_web` y `capacidad_sin_cotizacion` | Solo `bff-web` recibe los dos permisos y las dos variables, todo o nada (§3) |
| N1 (INT) | `infra/apps/tests/int/int.tftest.hcl` (con `-var-file=../envs/int/apps.tfvars`) | `cotizacion` con `min = 2`, `max = 4` y su política en 60/30/300; para el resto, invariantes relativos a sus overrides o a los globales (§2) |
| Unitarias | `services/bff-web/tests/unit/test_capacidad.py` | Dominio, adaptador con `botocore.stub.Stubber`, caso de uso, autorización por grupo y 503 ante fallos (§4) |
| Contrato | `services/bff-web/tests/contract/test_openapi.py::test_contrato_de_capacidad` | El OpenAPI de la ruta y sus esquemas (§4) |
| N2 | `tests/e2e/test_hu_w08_escalado.py`, escrita y no ejecutada; se lanza por `e2e.yml` con `-k hu_w08` | En INT: la alarma alta forzada sube `desiredCount` sin pasar del máximo y vuelve al mínimo sin bajar de él, con la línea de tiempo, las actividades y la tarea retirada (§5). Sus unitarios (`politica_de`, `linea_de_tiempo`, `seleccionada`) corren sin AWS |
