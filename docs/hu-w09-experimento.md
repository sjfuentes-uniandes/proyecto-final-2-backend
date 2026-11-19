# HU-W09 · Experimento de carga de la cotización

> Como responsable de producto quisiera que se ejecute una carga creciente de cotizaciones para comprobar el escalamiento de 500 a 50.000 solicitudes por minuto.

El experimento corre en `exp` con la cadena de R1 por socios (`api-socios`, `cotizacion`, `catalogo` y `clientes`). Una tarea Fargate de k6 carga el ALB interno con `experiments/hu-w09/carga.js`; otra da de alta el socio con `alta.js`. Son dos definiciones de tarea de `infra/apps/carga.tf` que llevan el script dentro (§5). Sin credenciales locales, todo se opera con workflows: **Infra - Desplegar** y **Infra - Destruir** para el ambiente, y **E2E (N3)** con `ambiente = exp` para las cuatro pruebas de `tests/e2e/test_hu_w09_carga.py`. Las decisiones están en `docs/plan-experimento-hu-w09.md` (§b), los criterios en `experiments/hu-w09/manifiesto.md` y la conclusión, en `experiments/hu-w09/informe.md`.

## 1. Protocolo

El job de **E2E (N3)** dura como máximo 20 minutos y la curva 30. Por eso, lanzar y recoger son ejecuciones separadas: la tarea de k6 sigue sola en Fargate.

| Paso | Qué se hace | Cómo |
|---|---|---|
| 1 | Comprobar que INT y `exp` están abajo y avisar al equipo | Plan §f, paso 1 |
| 2 | Levantar `exp` desde `main`, solo con la cadena | **Infra - Desplegar**, `servicios = api-socios,cotizacion,catalogo,clientes` |
| 3 | Preparar | **E2E (N3)**, `filtro = hu_w09_preparar` |
| 4 | Lanzar y anotar `t0` | **E2E (N3)**, `filtro = hu_w09_lanzar` |
| 5 | Esperar al menos 40 min desde `t0`, sin lanzar nada sobre `exp` | — |
| 6 | Recoger y copiar el resumen y el log a `experiments/hu-w09/resultados/` | **E2E (N3)**, `filtro = hu_w09_recoger` |
| 7 | Detener k6, solo si sigue viva | **E2E (N3)**, `filtro = hu_w09_detener` |
| 8 | Destruir `exp` | **Infra - Destruir**, confirmación `destruir exp` |

Los logs de k6 y de los servicios se borran con `exp`, así que hay que recoger antes de destruir. Las métricas de CloudWatch se pueden volver a consultar después.

## 2. Las pruebas

Cada una corre solo si el `-k` contiene su propio nombre. Con el filtro vacío o con `-k hu_w09`, se omiten siempre.

| Prueba | Duración | Qué hace en `exp` | Qué deja en el log | Qué deja en el resumen | Falla si |
|---|---|---|---|---|---|
| `hu_w09_preparar` | unos 10 min | Lanza la definición `<prefijo>-carga-alta` (`alta.js`, `startedBy = hu-w09-alta`) y mueve la API key del socio de `estandar` a `experimento` (decisión 2) | La cadena, la suscripción de alertas, las cuotas de vCPU de Fargate, el ARN del alta y un resumen en JSON | Socio, plan efectivo de la key, el movimiento, la suscripción, la URL del ALB con su origen, las cuotas de vCPU frente a la necesidad estimada y la tabla de la cadena | La cadena no está estable, el alta no termina con `exitCode` 0 o no aparece la API key. Que el cambio de plan falle no hace fallar la prueba: lo informa (opción c) |
| `hu_w09_lanzar` | pocos minutos | Lanza la definición `<prefijo>-carga` (`carga.js`, `startedBy = hu-w09`) | ARN, hora del lanzamiento y un resumen en JSON | ARN, definición, `t0`, la hora para recoger (`t0` + 40 min), el socio, el plan efectivo y la explicación del tramo `gw` | Ya hay una tarea `hu-w09…` viva, no aparece el socio o la tarea se detiene al arrancar |
| `hu_w09_recoger` | pocos minutos | Solo lee | Una línea `HU-W09-RECOGIDA {json}` con todo lo recogido | Las tablas de k6, CloudWatch, tareas, recursos, reacción del autoescalado, línea de tiempo, imágenes, el script de k6 (revisión, sha256 y si coincide con el del commit), evaluación preliminar y avisos | La tarea sigue corriendo («recoge después de …») o faltan datos: `t0`, las etapas o `OperationDuration` de `cotizacion` |
| `hu_w09_detener` | segundos | `StopTask` de toda tarea con `startedBy` `hu-w09…` | Qué detuvo | Qué detuvo | — |

## 3. Cómo se calcula lo que se recoge

- **Ventanas:** cada etapa va de `t0 + inicioS` a `t0 + inicioS + duracionS` (líneas `HU-W09-ETAPA`). CloudWatch se consulta sin su primer minuto y solo con minutos completos: de 1:00 a 5:00 de una etapa de 5 minutos, con un solo punto por consulta (el periodo es la ventana). Las tareas se cuentan por minuto (`SampleCount` de `CPUUtilization`, sin Container Insights).
- **Reacción del autoescalado**, por etapa y servicio (exploración E.14):
  - detección = `ALARM` de la alarma alta − inicio de la etapa (0 si ya estaba en `ALARM`);
  - decisión = inicio de la primera actividad que sube `desiredCount` − `ALARM`;
  - capacidad = `RUNNING` de la última tarea nueva de esa actividad − su inicio;
  - total = ese `RUNNING` − inicio de la etapa;
  - «al tope» si `desiredCount` ya estaba en el máximo al empezar la etapa.
- **Evaluación preliminar:** la regla de etapa válida y C2–C4 del manifiesto. No es la conclusión ni hace fallar la prueba. C1 (p95 de `cotizacion` ≤ 250 ms) se juzga en el informe con la tabla de CloudWatch (decisión 12).

## 4. Guardas y secretos

- Solo con `ENV=exp` y con las salidas de `infra/platform` de `exp` (clúster y prefijo `solventa-exp`). Si no, se omiten, o fallan con `E2E_REQUERIDO=1`, sin tocar nada.
- Las pruebas no comparten estado: cada una redescubre en AWS lo que necesita por convención de nombres. Si hay más de un ALB, SG, API key o cliente de Cognito que encaje, fallan con la lista. La excepción es el socio: `hu_w09_lanzar` toma el más reciente y nombra los demás.
- Nunca se imprimen el secreto del cliente, la API key, el `clientId` ni el id de la key. En Actions se enmascaran con `::add-mask::` antes de usarse, y todo lo que se imprime, se escribe en el resumen o va en un fallo pasa por un redactor, que además oculta la firma de cualquier URL prefirmada, como defensa.
- Las variables del socio viajan en `containerOverrides`, así que quedan visibles en `DescribeTasks` de `exp` mientras ECS conserve la tarea, cerca de una hora después de que se detiene.

## 5. Cómo llegan los scripts a k6

- **En la definición de tarea, no por una URL.** `infra/apps/carga.tf` lee cada script con `filebase64("${path.module}/../../experiments/hu-w09/<script>.js")` y lo deja en `HU_W09_SCRIPT`. El contenedor tiene fijos `entryPoint = ["sh", "-c"]` y el `command` `echo "$HU_W09_SCRIPT" | base64 -d > /tmp/s.js && exec k6 run --log-format=raw -q /tmp/s.js`. `-q` quita el progreso y el encabezado; los avisos de k6 y las líneas `HU-W09-*` siguen.
- **Por qué no S3 y una URL prefirmada:** el encabezado de k6 imprimía la URL entera, con la credencial, el token de sesión del rol de despliegue y la firma, y k6 le agrega `_k6` a la query string, lo que probablemente rompe la firma de S3 (decisión 5 del plan, revisada el 2026-11-19).
- **Límites:**
  - una definición de tarea admite 64 KiB (cuota de ECS no ajustable), y las pruebas de `infra/apps` exigen menos de 48 KiB para `container_definitions` (`carga.js` ocupa 23.616 bytes en base64);
  - las `overrides` de `run_task` admiten 8.192 caracteres, y la prueba lo comprueba antes de lanzar. Solo llevan `ALB_URL` (Terraform no conoce el DNS del ALB: platform no lo exporta) y las variables del socio.
- **Cambiar un script** crea una revisión nueva de su definición al aplicar `exp` desde `main`. `hu_w09_recoger` informa la revisión que corrió, el sha256 de su script y si coincide con el del commit.
- **Validación:** `infra-validar.yml` no se dispara con cambios solo en `experiments/**`. Tras cambiar un script, hay que correr a mano `terraform -chdir=infra/apps test`.
