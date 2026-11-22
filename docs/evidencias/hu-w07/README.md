# Evidencias de HU-W07 — réplicas independientes de la cotización

Corridas de la N3 (`tests/e2e/test_hu_w07_r1_replicas.py`) en INT, lanzadas con `E2E (N3)` (`e2e.yml`), `ambiente = int` y `filtro = hu_w07`. Cada archivo es **solo el paso «Pruebas E2E»** del log del job, sin el resto del archivo de logs:

- se quitaron los códigos de color ANSI y el BOM, y los finales de línea quedaron en LF;
- se redactó cualquier ID de cuenta (`<cuenta>`), correo (`<correo>`) e ID de API Gateway (`<api-id>`). En estos cuatro archivos no apareció ninguno;
- los secretos vienen enmascarados por GitHub (`***`): las tres credenciales temporales de AWS y `E2E_ADMIN_USUARIO`, `E2E_ADMIN_CLAVE` y `E2E_ADMIN_TOTP`. No hay ninguno sin enmascarar.

INT se desplegó para este ciclo desde `main` @ `002e6c1` (#89) el 2026-11-22, con `cotizacion` en 2 tareas. Entre `002e6c1` y `0f7045e` (#91) no cambia nada de `services/`, `libs/` ni `infra/`.

**No hay `e2e.xml`.** `e2e.yml` lo genera (`.github/workflows/e2e.yml:54`, `--junitxml=e2e.xml`), pero solo lo lee el paso «Resumen» para el resumen del job (`e2e.yml:56-73`). El workflow no tiene ningún paso `actions/upload-artifact`, así que el archivo no se conserva tras la ejecución.

## Corridas

Horas en UTC (inicio del paso). La N3 hace una ráfaga de 160 cotizaciones por el portal, una cada 0,5 s, y detiene una de las 2 tareas a mitad de la ráfaga.

| Archivo | Fecha | Rama @ SHA | Resultado | Qué demostró o qué hallazgo dejó |
|---|---|---|---|---|
| [`n3-int-2026-11-20-1-arranque-en-frio.txt`](n3-int-2026-11-20-1-arranque-en-frio.txt) | 2026-11-20 17:02 | `main` @ `4f3e746` | **Falla** (4 de 5 pasan) | **Arranque en frío.** 158 de 160 respuestas 201 y 2 respuestas 503, las 2 primeras de la ráfaga. Cada una tardó unos 2 s y llegó unos 38 s **antes** del retiro, fuera de la ventana de 30 s. El retiro no causó ninguna falla, y el reemplazo quedó listo en 73 s. Motivó el calentamiento (#89). |
| — | 2026-11-20 | `main` @ `4f3e746` | **Falla** | **Intento 1** del run `121222199535`, tras 39 minutos sin tráfico. Falló otra vez por el arranque en frío. Su log no está aquí: el archivo de logs descargado solo guarda el último intento del run. |
| [`n3-int-2026-11-20-2-intento-2.txt`](n3-int-2026-11-20-2-intento-2.txt) | 2026-11-20 17:44 | `main` @ `4f3e746` | **Pasa** (5 de 5) | **Intento 2** del mismo run, re-ejecutado y todavía sin calentamiento: 160 de 160 respuestas 201, el reemplazo listo en 71 s y 2 `task_id` en X-Ray. Muestra que el retiro no causa fallas y que el frío solo afecta a las primeras solicitudes tras la inactividad. |
| [`n3-int-2026-11-22-1-detenida-sin-stopped.txt`](n3-int-2026-11-22-1-detenida-sin-stopped.txt) | 2026-11-22 13:24 | `main` @ `002e6c1` (#89) | **Falla** (18 de 19 pasan) | Con el calentamiento: 4 de 4 respuestas 201 al calentar, 160 de 160 en la ráfaga y el reemplazo listo en 21 s. Falló en la última comprobación de AC2 (`assert nuestra and …` con `nuestra = []`). La prueba buscaba la detenida por su `stoppedAt`, y ECS no lo pone hasta que la tarea llega a STOPPED. Era un fallo de la prueba, no del servicio. Motivó #90. |
| [`n3-int-2026-11-22-2-verde.txt`](n3-int-2026-11-22-2-verde.txt) | 2026-11-22 13:40 | `fix/hu-w07/n3-tarea-detenida` @ `128e035` (fusionada como #90, `b4b32ac`, con el mismo archivo de prueba) | **Pasa** (24 de 24) | **La corrida de la 7.M.** Detalle abajo. |

## La corrida verde (2026-11-22, `128e035`)

Del resumen de la N3 (`HU-W07 N3, resumen para la evidencia`):

- **Calentamiento:** listo en 4 intentos, 4 de 4 respuestas 201, con latencias de 0,21 a 0,36 s.
- **Antes:** 2 tareas, en `us-east-1a` y `us-east-1b`.
- **Detenida:** la de `us-east-1b` (13:40:50). Llegó a `lastStatus` STOPPED con `stoppedReason` «HU-W07 N3», 106 s después del `stop_task` (`stoppedAt` 13:42:36).
- **Otras detenidas en la ventana:** ninguna. No hubo una interrupción de Spot que se confundiera con el retiro.
- **Reemplazo:** listo en 42 s, en la misma zona (`us-east-1b`). **Después:** de nuevo 2 tareas, en zonas distintas.
- **Ráfaga:** 160 de 160 respuestas 201, sin fallas (se permitía 1), y ninguna respuesta distinta de 201.
- **X-Ray:** 2 `solventa.task_id` distintos, los de las dos tareas de antes.

Qué cubre:

- **AC1**, réplicas independientes con un resultado equivalente: la ráfaga completa fue atendida por dos réplicas distintas (dos `task_id` en X-Ray) sin una sola falla.
- **AC2**, el retiro de una tarea: ECS la reemplazó en 42 s, sin 5xx, y la retirada terminó en STOPPED con el `stoppedReason` propio de la prueba.

## Observaciones y lo que queda abierto

- **El reemplazo no aparece en X-Ray en ninguna corrida.** Los `task_id` vistos son siempre los de las dos tareas de antes. AC1 pide al menos 2, y se cumple. No se investigó si es por el muestreo de X-Ray o porque la ráfaga termina antes de que el reemplazo reciba tráfico.
- **El arranque en frío** de la primera corrida es un hallazgo para el dueño de `cotizacion`, no de W07 (`docs/hu-w07-replicas.md` §5). La N3 lo evita ahora con el calentamiento. Las dos corridas del 2026-11-22 empezaron sin arranque en frío: el primer intento del calentamiento dio 201 en 0,4 s y en 0,24 s.
- **El 2026-11-20 hubo tres ejecuciones de la N3.** El run `121205364984` (17:02) y el intento 1 del run `121222199535`, tras 39 minutos sin tráfico, fallaron por el arranque en frío. El intento 2 de ese mismo run (17:44) pasó con 160 de 160.
