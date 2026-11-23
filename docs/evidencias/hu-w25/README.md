# Evidencias de HU-W25 — escalado de consumidores por atraso de colas

Corrida de la N2 (`tests/e2e/test_hu_w25_escalado.py`) en INT sobre `pagos-parametricos`, lanzada con `E2E (N3)` (`e2e.yml`), `ambiente = int` y un filtro por fase (`hu_w25_publicar`, `hu_w25_recoger`). HU-W25 ya estaba fusionada en `main`; el ambiente se destruyó al terminar. Cada archivo conserva solo del paso «Pruebas E2E» del log:

- las líneas `HU-W25 …`, `HU-W25-PUBLICACION` y `HU-W25-RECOGIDA`;
- la línea de la prueba (`PASSED`), `PASSED …` del resumen corto y el resumen final de pytest.

No es el log completo. Se quitaron los códigos de color ANSI, el BOM y los `\r`. Las horas son UTC: la corrida empezó a las 03:50 UTC del 2026-11-23, es decir, la tarde-noche del 2026-11-22 en Colombia.

**Enmascarado:** nada. Se buscaron ARN, IDs de cuenta de 12 dígitos y correos en lo extraído, y no hay ninguno. Los números largos que aparecen son sufijos de corrida (fecha y hora) y el ID de un despliegue de ECS (`ecs-svc/…`), que no es una cuenta. Los secretos del paso vienen enmascarados por GitHub (`***`) en el bloque `env`, que no se copió.

**No hay `e2e.xml`:** `e2e.yml` no sube artefactos, así que la evidencia es el log de cada run.

## Corridas

| Archivo | Run | Duración | Resultado |
|---|---|---|---|
| [`n2-int-2026-11-22-1-publicar.txt`](n2-int-2026-11-22-1-publicar.txt) | 122067643018 | 5 min 26 s | `hu_w25_publicar` pasa |
| [`n2-int-2026-11-22-2-recoger-1.txt`](n2-int-2026-11-22-2-recoger-1.txt) | 122070654249 | 10 min 23 s | `hu_w25_recoger` pasa; aún no volvió al mínimo (`volvioAlMinimo false`) |
| [`n2-int-2026-11-22-3-recoger-2.txt`](n2-int-2026-11-22-3-recoger-2.txt) | 122074141970 | 1 min 11 s | `hu_w25_recoger` pasa; `volvioAlMinimo true` |

## Qué demuestran

- **Subida de 1 a 3 tareas.** La ráfaga de 2.000 eventos arrancó a las 03:51:00. El ritmo por tarea fue de 7,55 mensajes/s (453 borrados en el primer minuto con 1 tarea), así que la ráfaga sola bastó: complemento 0. `desiredCount` pasó de 1 a 3 a los **289 s** de la ráfaga, pedida por `solventa-int-pagos-parametricos-backlog` (T = 10 visibles por tarea, enfriamientos de 60 s y 300 s).
- **Retiro de 3 a 2 y luego a 1.**
  - De 3 a 2 a las 04:10:57, por la política `backlog`. La tarea retirada salió con `exitCode 0` en `pagos-parametricos` y en `otel-collector`, y tardó **99,6 s** de `stoppingAt` a `stoppedAt`.
  - Tras la segunda recogida, `desiredCount` llegó a 1 (`volvioAlMinimo true`; el `runningCount` seguía en 2 mientras la última tarea terminaba de pararse).
- **Reconciliación** (por `eventoId`, en la auditoría y las dos DLQ): en la recogida final, **2.662 publicados y 2.662 evaluados** (centinela, ráfaga y los dos goteos de 600 y 61), **0 faltantes, 0 repetidos** y **delta de las DLQ 0** (`parametricos-eventos` y `auditoria`). Todas las decisiones fueron `SIN_POLIZAS`. En la primera recogida eran 2.601 y 2.601.
- **H-3 coincide.** `RunningTaskCount` y los eventos de ECS coinciden en 31 minutos (racha máxima de 1 min con más de 1 de diferencia; 1 minuto sin alguno de los dos datos).
- **Liberados: 0** («inbox: mensaje liberado por la parada»). Se informa y no se exige: lo exigen el módulo y el SIGTERM de Linux (25.5).

## Observaciones

1. **Mensaje inexacto de la primera recogida.** `hu_w25_recoger` anunció «vuelve a lanzar … a las 04:18:00 (sin goteo, solo recoge)», pero la segunda corrida sí publicó un goteo de **61 eventos** (04:15:59–04:16:59, `eventoOrigen` propio). El goteo se declaró y se reconcilió (están entre los 2.662), así que el resultado no cambia; solo el texto del mensaje es inexacto. No se cambió el código.
2. **Retiro de 2 a 1 sin `exitCode`.** El log de la segunda recogida repite el retiro de 3 a 2 y no registra la tarea ni el `exitCode` de la reducción de 2 a 1: solo consta el resultado (`desiredCount 1`, `volvioAlMinimo true`). No se cambió el código.
