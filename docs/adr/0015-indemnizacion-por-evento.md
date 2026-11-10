# 0015. La indemnización la paga `dinero` por evento, una sola vez por siniestro

- Estado: propuesto
- Fecha: 2026-11-10 · Decide: por ratificar en la revisión del PR del backend de HU-W23 (mismo procedimiento que las ADR 0012 y 0013) · Historias/ASR: HU-W20, HU-W23 (y HU-W22 para el recorrido R5); recorridos R5 y R7; ARQ-001
- Fuente: plan maestro del Sprint 3 (v1.4), decisiones DS3-17, DS3-18, DS3-24, DS3-25 y DS3-26, y el conflicto CF-S3-02. Backend `main` @ `7b6e5d0`

## Contexto

- El diagrama VA-003A (`diagrama_componentes_responsabilidades.puml`) dibujaba `Siniestros → Dinero : REST solicita indemnización idempotente`. Los issues de HU-W20, HU-W22 y HU-W23 y la infraestructura de HU-S03 lo resuelven **por evento**, y dejan `siniestros.calls = []`:
  - `siniestros` publica `SiniestroAprobado` (HU-W20) en el tópico de negocio.
  - La cola `dinero-indemnizaciones` (filtro `eventType = SiniestroAprobado`, visibilidad de 60 s) la consume `dinero`, que llama solo a `adaptador-pagos` (`infra/platform/catalog.tf`).
  - La cola `siniestros-indemnizaciones` (prefijo `Indemnizacion`, visibilidad de 30 s) la consume `siniestros` para reflejar el resultado.
- Es la misma situación de la ADR 0013 para el recaudo: la infraestructura ya declara el diseño asíncrono y el diagrama dibuja otra cosa. Mandan los criterios de aceptación del issue y la infraestructura.
- `SiniestroAprobado.v1` es único para R5 y R7 (DS3-24): lo publica `siniestros` tanto cuando aprueba un analista (HU-W20) como cuando nace aprobado un siniestro paramétrico (HU-W22). Trae `referenciaPago` (`IND-<codigo>-01`) y `beneficiarioRef`, un identificador opaco: **nunca la cuenta**.
- `dinero` ya tiene el patrón del recaudo (HU-W16, ADR 0013): movimiento `PENDIENTE` con commit **antes** de llamar al tercero, resultado definitivo en otra transacción, un incierto que nunca se toma como rechazo y una conciliación periódica.
- El issue dice que se reutiliza «el modelo de movimientos de HU-W16 con `tipo = INDEMNIZACION`». La tabla `recaudos` no tiene columna de tipo, su clave única es `clave_recaudo` y tiene un disparador de protección: no admite el tipo sin migrarla en caliente.

## Decisión

1. **Disparo por evento (CF-S3-02).** `siniestros → dinero` es `SiniestroAprobado` por SNS y SQS, no REST. Se cambia la flecha del diagrama VA-003A y se regenera solo esa imagen. `dinero` responde con `IndemnizacionConfirmada` o `IndemnizacionRechazada` (por la misma vía) y `siniestros` actualiza el resultado del caso.
2. **Agregado propio (DS3-17).** `dinero` tiene `domain/indemnizacion/` con la forma de `Recaudo` (estados `PENDIENTE`, `CONFIRMADA` y `RECHAZADA`; `registrar_incierto`; `registrar_duplicado`) y una tabla nueva `indemnizaciones`: `tipo` fijo `INDEMNIZACION`, `UNIQUE (siniestro_id, tipo)`, `referencia` única y un disparador `indemnizaciones_protegidas` igual a `recaudos_protegidos`. **No se modifica la tabla `recaudos`.** Se reutiliza el **patrón** (estados, consumidor, conciliación), no la tabla.
3. **Una sola indemnización, una sola llamada de pago.**
   - La clave única es `(siniestro_id, tipo)`: el mismo evento dos veces (mismo `eventId`), otro `eventId` del mismo siniestro, o dos consumidores a la vez producen **una** fila. El consumidor inserta `PENDIENTE` con `ON CONFLICT (siniestro_id, tipo) DO UPDATE SET duplicados_descartados = duplicados_descartados + 1`, sin tocar nada más; solo quien inserta la fila nueva llama a la pasarela.
   - `referencia` (`IND-<codigo>-01`) es la clave idempotente hacia la pasarela. Una confirmación repetida sobre una fila ya `CONFIRMADA` solo suma un duplicado descartado: no hay otro movimiento ni otra llamada efectiva.
4. **Incertidumbre (ADR 0013, decisión 5).** La llamada a `adaptador-pagos` va **después** del commit y fuera de toda transacción (regla 8 de §13), en la continuación del consumidor en el modo (b). Un timeout, un 5xx o una respuesta inválida dejan la fila `PENDIENTE` (`registrar_incierto`): nunca se asume rechazo. Solo una respuesta definitiva la lleva a `CONFIRMADA` (con `transaccionId`) o `RECHAZADA` (con el motivo normalizado).
5. **Conciliación con varias réplicas (DS3-18).** Es una tarea periódica en proceso, propia de las indemnizaciones (la de recaudos de W16 concilia «sin `FOR UPDATE`»; aquí el issue exige `SKIP LOCKED`):
   - En una transacción corta reclama un lote de `PENDIENTE` con antigüedad ≥ `antiguedad_min_s`, con `FOR UPDATE SKIP LOCKED`, y marca `reclamado_hasta = ahora + 60 s`. Mientras no venza, ninguna otra réplica toma el movimiento.
   - **Después del commit** consulta `GET /v1/desembolsos/{referencia}` en `adaptador-pagos` y aplica el resultado en otra transacción corta. Si el proveedor no conoce la referencia, reintenta el desembolso con la misma `referencia` (idempotente).
   - Operación `indemnizacion.conciliar`. La alarma de antigüedad de pendientes se publica con la dimensión `tipo`, para que HU-S03 extienda `pending_collection_age_seconds`.
6. **Resultado en `siniestros` (DS3-25).** `siniestros` consume `siniestros-indemnizaciones` en el modo (a) y guarda el resultado en las columnas `pago_*` que existen desde HU-W19 (sin migración), con su entrada de historial (`PAGO_CONFIRMADO` o `PAGO_RECHAZADO`) y el paso «Pago de la indemnización» del trámite. Un `Confirmada` repetido o fuera de orden no cambia nada. `RECHAZADO` se presenta como «Pago por gestionar» (AC4). **No reemite eventos:** `consulta` proyecta los `Indemnizacion*` directamente.
   - Mientras `dinero` no ha publicado un resultado, un caso `APROBADO` muestra `pago.estado = PENDIENTE`: se **deriva** al leer (no se escribe), porque ningún evento marca el pago como pendiente.
7. **Vista de H08 (DS3-26, plan B).** HU-S03 no agregó `bff-web → dinero` (EXT-S3-02). `bff-web` arma `GET /v1/siniestros/{siniestroId}/pago` solo con `siniestros`, que guarda lo que llega en los eventos: por eso `IndemnizacionConfirmada` e `IndemnizacionRechazada` llevan también `intentos` y `duplicadosDescartados`. `GET /v1/indemnizaciones?siniestroId=` de `dinero` se implementa igual; cuando las llamadas existan (sincronización final, SYNC-01), la vista se compone con `dinero` **sin cambiar el contrato** del portal.
8. **Desembolsos en `adaptador-pagos`.** `POST /v1/desembolsos` y `GET /v1/desembolsos/{referencia}` con el estilo exacto de los cobros: la clave idempotente (`referencia`) viaja en el cuerpo y se reenvía al aliado como `Idempotency-Key`; sin reintentos ni lógica de negocio; mismas respuestas de error (504 `TIEMPO_AGOTADO`, 503 `FUENTE_NO_DISPONIBLE`, 502 `RESPUESTA_INVALIDA`). La consulta lleva `beneficiarioRef` (como la de los cobros lleva `pagadorRef`) porque el aliado simulado no tiene memoria y elige el escenario por el beneficiario sintético.
9. **Sin datos de la cuenta.** Ningún log, traza, métrica, error ni evento lleva la cuenta del beneficiario ni su nombre: solo `beneficiarioRef` y la referencia del pago. El aliado de pagos es siempre el simulador.

## Alternativas descartadas

- **`siniestros → dinero` por REST (VA-003A):** `siniestros.calls` está vacío y `dinero` no es descubrible desde ningún servicio de negocio; exigiría cambiar `infra/`. Además crea un acoplamiento síncrono que el evento no necesita.
- **Un tipo nuevo en la tabla `recaudos`:** hay que migrar en caliente una tabla de producción de HU-W16, cambiar su clave única y su disparador, y el `UNIQUE (siniestro_id, tipo)` no cabe en una tabla cuya clave es `clave_recaudo`.
- **Llamar a la pasarela dentro de la transacción del consumidor:** retendría una conexión de base mientras espera a un tercero (regla 8) y un timeout dejaría un rollback sin rastro de la orden.
- **Reutilizar la conciliación de recaudos:** consulta sin `FOR UPDATE`; con varias réplicas, dos tareas podrían consultar a la vez el mismo movimiento. El issue de HU-W23 exige `SKIP LOCKED`.
- **Que `siniestros` escriba `PENDIENTE` al aprobar:** tocaría la transacción de la decisión de HU-W20 y dejaría mal los casos ya aprobados. Derivarlo al leer es aditivo y cubre también los anteriores.

## Consecuencias

- **Se gana:**
  - El diseño coincide con la infraestructura ya desplegable y con los criterios de HU-W23 (AC1 a AC4).
  - «Cero pagos duplicados» se prueba con la fila única y con el registro de llamadas de WireMock.
  - HU-W22 (R5) reutiliza el mismo pago sin cambios.
- **Se sacrifica:**
  - La consistencia del recorrido es eventual: tras aprobar, el pago aparece unos segundos después (el relay y los consumidores).
  - La vista de H08 en el plan B no puede mostrar nada que `siniestros` no guarde desde los eventos (por ejemplo, `ultimoIntentoEn` de un pendiente).
- **Hay que cambiar (otra persona, en `infra/`), en la sincronización final (§2.7, SYNC-01):**
  - `bff-web.calls` += `dinero`, para componer H08 con `dinero`.
  - `dinero` figura con `listener_port = null` en `infra/platform/catalog.tf`: la ruta `GET /v1/indemnizaciones` solo es alcanzable en local y en las pruebas hasta que tenga un *listener*.
- Si el equipo no ratifica esta ADR, se retira el cambio del diagrama VA-003A (la flecha REST) y se registra en la bitácora del Sprint 3; el código no cambia, porque ya sigue la infraestructura.
