# Evidencias de HU-W08 — autoescalado de la cotización

Corridas de la N2 (`tests/e2e/test_hu_w08_escalado.py`) en INT, lanzadas con `E2E (N3)` (`e2e.yml`), `ambiente = int` y `filtro = hu_w08`. La N2 fuerza la alarma alta de la política de *target tracking* de `cotizacion` (`SetAlarmState`) y observa cómo App Auto Scaling sube una tarea y la vuelve a bajar. Cada archivo es **solo el paso «Pruebas E2E»** del log del job, sin el resto del archivo de logs:

- se quitaron los códigos de color ANSI y el BOM, y los finales de línea quedaron en LF;
- se redactó cualquier ID de cuenta (`<cuenta>`), correo (`<correo>`) e ID de API Gateway (`<api-id>`). Solo apareció uno: la cuenta en el ARN de la política de escalado del historial de la alarma, en `n2-int-2026-11-22-3-verde.txt`;
- los secretos vienen enmascarados por GitHub (`***`): las tres credenciales temporales de AWS y `E2E_ADMIN_USUARIO`, `E2E_ADMIN_CLAVE` y `E2E_ADMIN_TOTP`. No hay ninguno sin enmascarar.

INT se desplegó para este ciclo desde `main` @ `002e6c1` (#89) el 2026-11-22. Entre `002e6c1` y `0f7045e` (#91) no cambia nada de `services/`, `libs/` ni `infra/`.

**No hay `e2e.xml`.** `e2e.yml` lo genera (`.github/workflows/e2e.yml:54`, `--junitxml=e2e.xml`), pero solo lo lee el paso «Resumen» para el resumen del job (`e2e.yml:56-73`). El workflow no tiene ningún paso `actions/upload-artifact`, así que el archivo no se conserva tras la ejecución.

## Corridas

Horas en UTC (inicio del paso).

| Archivo | Fecha | Rama @ SHA | Resultado | Qué demostró o qué hallazgo dejó |
|---|---|---|---|---|
| [`n2-int-2026-11-20-sin-state-reason-data.txt`](n2-int-2026-11-20-sin-state-reason-data.txt) | 2026-11-20 17:51 | `main` @ `4f3e746` | **Falla** (13 de 14 pasan) | La alarma alta pasó a `ALARM`, pero el historial dice «Failed to execute AutoScaling action: StateReasonData must be provided.». No hubo ninguna actividad de escalado, y `desiredCount` siguió en 2 durante 180 s. Era un error de la llamada de la prueba, no el riesgo de *target tracking*. Motivó #83 (`f09842e`). |
| [`n2-int-2026-11-22-1-sin-scalable-target.txt`](n2-int-2026-11-22-1-sin-scalable-target.txt) | 2026-11-22 01:44 | `main` @ `f44268c` | **Error de la guarda** (23 pasan, 1 error) | «service/solventa-int/cotizacion no tiene scalable target (0)». La N2 se lanzó cuando INT todavía no estaba desplegado en este ciclo, que se desplegó a las 09:53. La guarda cortó antes de forzar nada. No es un hallazgo. |
| [`n2-int-2026-11-22-2-sin-metric-data-points.txt`](n2-int-2026-11-22-2-sin-metric-data-points.txt) | 2026-11-22 13:30 | `main` @ `002e6c1` (con #83) | **Falla** (23 de 24 pasan) | Con el `StateReasonData` de #83 (claves con mayúscula, como el ejemplo de `SetAlarmState`), el historial dice «Failed to execute AutoScaling action: Metric data points must be provided». La alarma volvió sola a `OK` a los 30 s, sin escalar. Motivó #91. |
| [`n2-int-2026-11-22-3-verde.txt`](n2-int-2026-11-22-3-verde.txt) | 2026-11-22 13:53 | `fix/hu-w08/n2-formato-datos` @ `50f2367` (fusionada como #91, `0f7045e`, con el mismo archivo de prueba) | **Pasa** (34 de 34) | **La corrida de la 8.M.** Detalle abajo. |

## La corrida verde (2026-11-22, `50f2367`)

Del resumen de la N2 (`HU-W08 N2, resumen para la evidencia`):

- **Política `solventa-int-cotizacion-cpu`:** objetivo 60 %, enfriamientos de 30 s (salida) y 300 s (entrada), `MinCapacity` 2 y `MaxCapacity` 4, sin nada suspendido. Son los valores que fija W08 (globales 60/30/300 y `max = 4` en INT).
- **Plantilla del `StateReasonData`:** las claves que CloudWatch dejó en la alarma alta, en camelCase minúscula (`version`, `queryDate`, `startDate`, `unit`, `statistic`, `period`, `recentDatapoints`, `threshold` y `evaluatedDatapoints[timestamp, sampleCount, value]`). Confirma la hipótesis de #91.
- **Valor enviado:** 82,5 % con umbral 60, el que pide exactamente una tarea más desde 2 (2 × 82,5 / 60 = 2,75).
- **Alarma alta forzada** a las 13:53:02. El historial muestra «Successfully executed action» sobre la política (ARN con la cuenta redactada), y la alarma volvió a `OK` a las 13:53:48.
- **Línea de tiempo** (segundos desde la alarma; tareas deseadas, corriendo y pendientes): `(0,1; 2, 2, 0)`, `(10,2; 3, 2, 1)`, `(91,3; 3, 3, 0)` y `(131,9; 2, 2, 0)`.
  - **Subida:** las deseadas pasan a 3 **a los 10 s**, y la tercera tarea está corriendo **a los 91 s**.
  - **Bajada:** las deseadas y las que corren vuelven a 2 **a los 132 s**, 122 s después de la subida. Lo dispara la alarma baja, que estaba en `ALARM` desde las 10:12 porque la CPU está en reposo. Según la guía de *target tracking*, el enfriamiento de 300 s solo bloquea una reducción después de otra reducción.
- **Actividades de escalado:** «Setting desired count to 3.» (13:53:02 → 13:54:37, `Successful`) y «Setting desired count to 2.» (desde las 13:55:04, `InProgress` al terminar la prueba).
- **Tarea retirada por la bajada:** una, con `stopCode` `ServiceSchedulerInitiated`.
- **Estado final:** la alarma alta en `OK`, `desiredCount` 2 y `runningCount` 2, y el *scalable target* y la política sin tocar.

Qué cubre:

- **AC1:** la política de CPU de `cotizacion` escala hacia fuera cuando se supera el objetivo.
- **AC2:** dentro de los límites (2 a 4), una sola tarea más, y luego vuelve al mínimo.

La alarma se forzó, no se cargó la CPU de verdad. La validación con carga real es de HU-W09.

## Capturas del tablero

Cuatro capturas de Operación › Tablero en INT, con un usuario del grupo `operacion`, copiadas tal cual. La cabecera muestra el correo de la cuenta con la que se entró: se dejó visible a propósito. Horas en hora local (UTC−5).

| Archivo | Hora | Idioma | Qué muestra |
|---|---|---|---|
| [`tablero-es-co-2026-11-22-0830.png`](tablero-es-co-2026-11-22-0830.png) | 08:30 (13:30 UTC) | es-CO | El panel en español: «Cotizaciones por minuto y capacidad», «Pico observado 114 / min», «Tareas activas 2 de 4» y «Tiempo de escalado: sin escalados en la ventana». Es de **antes** de la N2 verde, durante la corrida fallida de las 13:30, así que no hay escalado en la ventana. |
| [`tablero-en-us-2026-11-22-0830.png`](tablero-en-us-2026-11-22-0830.png) | 08:30 (13:30 UTC) | en-US | El mismo momento en inglés: «Quotations per minute and capacity», «Observed peak 114 / min», «Active tasks 2 of 4» y «no scaling in the window». |
| [`tablero-en-us-2026-11-22-0856.png`](tablero-en-us-2026-11-22-0856.png) | 08:56 (13:56 UTC) | en-US | **Después** de la N2 verde: «Observed peak 153 / min», «Active tasks 3 of 4», «Scaling time 34 sec» y el aviso «The last scaling started at 08:55 AM and took 34 seconds», con la leyenda del escalado. |
| [`tablero-en-us-2026-11-22-0857.png`](tablero-en-us-2026-11-22-0857.png) | 08:57 (13:57 UTC) | en-US | Lo mismo un minuto después, con la barra del último escalado ya resaltada (rayada). |

**El escalado lo muestran las capturas en en-US.** La de es-CO muestra el panel en español, sin escalado en la ventana. No se tomó una en es-CO después del escalado: la traducción completa del panel la cubren las pruebas unitarias de i18n del tablero, en el frontend:

- `web/src/app/features/operacion/tablero/tablero.i18n.spec.ts:11`: los tres idiomas (es-CO, en-US y pseudo) tienen las mismas claves;
- `tablero.i18n.spec.ts:22`: en-US traduce todos los textos visibles;
- `web/src/app/features/operacion/tablero/tablero.component.spec.ts:353`: en en-US, el componente traduce los textos y formatea números y unidades en inglés.

Notas:

- El último escalado que muestra el panel (08:55 local, 13:55 UTC) es la **bajada**, la actividad de las 13:55:04. No es la subida de las 13:53.
- «Active tasks 3 of 4» a las 13:56 y 13:57 se explica por cómo se calcula el dato: es el `SampleCount` de `CPUUtilization` del último minuto (`services/bff-web/src/bff_web/adapters/outbound/capacidad.py:7`), y en ese minuto todavía reportaba la tarea que se estaba retirando.
- La navegación lateral («Trazas», «Alertas», «Continuidad») y la superior no se traducen en en-US. Son del armazón del portal, fuera del diccionario de la página nueva (plan de W08, §b, decisión 9).
