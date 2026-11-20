# HU-W29 · Informe del experimento multizona

> **Estado: pendiente de ejecución.** El experimento se ejecuta con la confirmación del despliegue de `exp` (guía de verificación de los AC). Esta plantilla se llena con la evidencia de `resultados/` y se contrasta con el objetivo de [manifiesto.md](manifiesto.md).

| | |
| --- | --- |
| Experimento FIS | `<resultados/experimento.txt>` |
| Fecha y ventana (UTC) | `<desde> – <hasta>` |
| Commit y digests | ver manifiesto, sección 4 |
| Conclusión ARQ-001 | **`<CUMPLIDO / NO CUMPLIDO>`** |

## 1. Distribución de tareas por zona

Fuente: `resultados/distribucion-{antes,durante,despues}.json`.

| Servicio | Antes (A / B) | Durante (A / B) | Después (A / B) |
| --- | --- | --- | --- |
| bff-web | | | |
| bff-movil | | | |
| api-socios | | | |
| cotizacion | | | |
| polizas | | | |
| dinero | | | |
| siniestros | | | |
| pagos-parametricos | | | |
| Base principal (zona) | | | |

## 2. Cronología

Fuente: `resultados/fis-experimento.json`, `alarmas.json` y `cronologia.ndjson`.

| Hora (UTC) | Δ desde la inyección | Evento | Fuente |
| --- | --- | --- | --- |
| | 0 s | Inicio de `InterrumpirZonaA` y `FailoverBase` | FIS |
| | | Primeras tareas de la zona A no saludables | ECS |
| | | Alarma en ALARM (detección) | CloudWatch |
| | | Failover de RDS completado (standby promovido) | RDS |
| | | Carga estable bajo 1 % de error (redirección) | `informe.json` |
| | 10 min | Fin de la interrupción; ACL restauradas | FIS |
| | | Tareas de reemplazo saludables en la zona A | ECS |
| | | Alarmas en OK (recuperación) | CloudWatch |

## 3. Errores en la ventana

Fuente: `informe.json` (`recorridos`, `continuidad.serie`).

| Recorrido | Intentos | Confirmadas | Errores | Por estado |
| --- | --- | --- | --- | --- |
| R1 | | | | |
| R3 | | | | |
| R5 | | | | |

## 4. Reconciliación

Fuente: `informe.json` (`verificaciones`, `dlq`).

| Verificación | Esperado | Observado | Resultado |
| --- | --- | --- | --- |
| Cotizaciones confirmadas registradas una vez | | | |
| Aceptaciones confirmadas registradas una vez | | | |
| Cada aceptación con un cobro | | | |
| Eventos de telemetría recibidos una vez (R5) | | | |
| Cobros duplicados | 0 | | |
| Eventos de telemetría evaluados una vez (R5) | | | |
| Cada activación con su siniestro paramétrico (R5) | | | |
| Siniestros paramétricos duplicados | 0 | | |
| Indemnizaciones duplicadas | 0 | | |
| DLQ sin explicación | 0 | | |
| Outbox pendiente | 0 | | |

## 5. Contraste con el objetivo

| # | Meta (manifiesto) | Medido | ¿Cumple? |
| --- | --- | --- | --- |
| O1 | Redirección ≤ 10 min | | |
| O2 | 0 perdidas | | |
| O3 | 0 duplicadas | | |
| O4 | 0 cobros duplicados | | |
| O5 | 0 en DLQ sin explicación | | |
| O6 | Cronología completa | | |

## 6. Costo

| Concepto | Estimado (manifiesto) | Real (Cost Explorer, etiqueta Environment = exp) |
| --- | --- | --- |
| Ambiente | ≈ 1,0 USD/h × `<h>` | |
| FIS | ≈ 1,1 USD | |

## 7. Conclusión y desviaciones

`<Cumplido o no cumplido para ARQ-001, con la razón; desviaciones respecto al manifiesto.>`

## 8. Destrucción de EXP

`<make infra-destruir ENV=exp: hora y salida; comprobación de que no quedan recursos solventa-exp-*.>`
