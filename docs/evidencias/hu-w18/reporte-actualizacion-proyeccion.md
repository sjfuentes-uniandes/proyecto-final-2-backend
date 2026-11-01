# HU-W18: reporte de actualización de la proyección

Cadena N2 (`services/bff-web/tests/module/test_n2_proyeccion_w18.py`): SQS (LocalStack 4.14.0) →
consumidor de `consulta` (inbox + proyección, PostgreSQL 16) → `GET /v1/polizas/{id}/estado` vía `bff-web`.
Eventos sintéticos conformes a `PolizaEmitida.v1`.

| Paso | Observado |
|---|---|
| Antes del evento | 404 `POLIZA_NO_ENCONTRADA`, cuerpo idéntico al de una póliza inexistente (salvo `correlationId`) |
| Evento publicado (vigencia desde mañana) | proyectado; 200 con `estado = NO_INICIADA` |
| `fechaActualizacion` | igual a `proyectada_en` de la fila |
| Desfase `emitida_en` → `proyectada_en` | 0,115 s (corrida local; el consumidor espera hasta 1 s en la prueba, 10 s en AWS) |
| Mismo evento dos veces | una sola fila en `poliza_cliente`; cola vacía |
| Póliza ya vigente / por vencer | 200 `VIGENTE` / 200 `POR_VENCER` |
| Listado | incluye la póliza; `total` +1; `actualizadoEn` avanza |

Si la proyección aún no recibió un cambio, se muestra el último estado conocido con su fecha. El estado se
deriva de la vigencia con la fecha de Bogotá, de modo que cambia de día sin necesidad de un evento.
