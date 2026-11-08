# HU-W24 · Eventos paramétricos con particiones

Issue: [sjfuentes-uniandes/proyecto-final#66](https://github.com/sjfuentes-uniandes/proyecto-final/issues/66). Recorrido R5 (el volumen se mide en HU-W26). Pantallas H10 y H11 (portal).

Los eventos se consumen con contrapresión, sin perder mensajes y con el mismo resultado sin importar el orden o los duplicados. La DLQ de `parametricos-eventos` se puede ver y reprocesar desde el portal.

## 1. Diseño en una página

- **Partición (D2):** una cola SQS estándar por grupo de consumidores, suscrita al tópico de eventos de negocio con su filtro, su política de reintentos (`maxReceiveCount` = 5) y su DLQ (14 días). La procesan N tareas en paralelo. Sin FIFO y sin garantía de orden. Respaldo: `diagrama_despliegue_ecs_fargate.puml` ("SNS + SQS: fan-out, desacoplamiento, backpressure y reintento"; `SQS -right-> DLQ : reintentos agotados`) y `modelo_concurrencia.puml` (`ParticionesEventos --> ConsumidoresParametricos`). Las colas las dejó HU-S03 (`infra/platform/catalog.tf`); `parametricos-eventos` ya retiene 4 días.
- **Orden:** no se necesita. Lo sustituyen el Inbox por `eventId`, la clave única de negocio y la regla de `aggregateVersion` (sección 3). Respaldo: la nota de `modelo_concurrencia.puml`, "La entrega puede repetirse; el efecto del negocio no".
- **Contrapresión:** el consumidor común de `solventa_common.inbox` (sección 2): espera larga de 20 s, lotes de hasta 10, concurrencia limitada por tarea (`SQS_CONCURRENCIA`) y latido de visibilidad. El mensaje se borra solo después del commit.
- **Auditoría (D3):** `auditoria` sigue recibiendo todos los eventos y escribe un objeto por lote en S3, encadenado por `previousHash` (sección 4).
- **DLQ visible (H10, H11):** `pagos-parametricos` lista, reprocesa, corrige y descarta los mensajes de `parametricos-eventos-dlq`, y `bff-web` lo expone al grupo `analistas-siniestros` (sección 5).

## 2. Consumidor común (para capbmaster y Santi-MISO)

Es el mismo `ConsumidorInbox` de HU-W16, extendido. Sin los parámetros nuevos se comporta como antes: un lote en secuencia por ciclo. Así `dinero`, `polizas` y `consulta` no cambian. Las opciones nuevas:

| Parámetro | Qué hace | Valor sugerido |
| --- | --- | --- |
| `concurrencia` | Mensajes a la vez por tarea (`SQS_CONCURRENCIA`). Nunca tiene más recibidos sin terminar: pide a SQS solo los huecos libres (como mucho 10) y, sin huecos, no recibe. **Esta es la contrapresión (AC1).** | 10 para un manejador de I/O corto |
| `visibilidad_s` | La visibilidad de la cola. Activa un latido por mensaje que la renueva cada `visibilidad_s / 2`, desde que se recibe hasta que termina (`ChangeMessageVisibility`). | La de `catalog.tf` (30 s en `parametricos-*`) |
| `plazo_parada_s` | Tiempo que tienen los mensajes en curso para terminar al recibir la parada; después se cancelan (rollback) y se liberan. | 10 s (ECS da 30 s entre SIGTERM y SIGKILL) |
| `al_fallar` | `async (mensaje, evento, desenlace)`, se llama tras cada desenlace que no borra (envenenado o fallo), fuera de la transacción. Sirve para anotar la causa que muestra la DLQ. | Ver `pagos-parametricos` |

`concurrencia` y `visibilidad_s` piden un `ReceptorControlado`: `ReceptorSqs` lo es (`recibir_hasta`, `extender`, `liberar`). Úsalo con `max_mensajes=10`.

**Parada (SIGTERM, scale-in de HU-W25):** uvicorn convierte el SIGTERM en el cierre del lifespan. El servicio pone su `asyncio.Event` de parada y el consumidor:
1. deja de recibir (una espera larga en curso se abandona; lo que traiga se libera);
2. libera con visibilidad 0 los mensajes recibidos que aún no empezaron, para que otra tarea los tome enseguida;
3. espera hasta `plazo_parada_s` a los que están en curso; si no terminan, los cancela (rollback, sin fila en el inbox) y los libera.

Ningún mensaje se pierde ni se borra sin commit. Como lo liberado no espera la visibilidad, el drenaje de HU-W25 solo necesita esperar a que `ejecutar` termine.

**Medición:** cada mensaje es una `operacion("inbox.consumir")`, con span, `OperationDuration`, `OperationErrors` y log. El log lleva `message_id`, `receive_count`, `outcome` (`nuevo`, `duplicado`, `envenenado`, `fallo_transaccion`, `fallo_continuacion`), `event_id`, `event_type` y `correlation_id`. El `correlationId` del sobre queda en el contexto para el manejador y la continuación; sus logs llevan también `operation = inbox.consumir`.

### 2.1 Ejemplo

```python
from solventa_common.inbox import ConsumidorInbox, InboxPg, ReceptorSqs, cliente_sqs

receptor = ReceptorSqs(
    cliente_sqs(settings.aws_region, endpoint_url=settings.aws_endpoint_url),
    settings.sqs_parametricos_eventos_url,
    max_mensajes=10,
)
consumidor = ConsumidorInbox(
    receptor,
    InboxPg(pool),
    manejador,                       # async (evento, tx) -> Continuacion | None, como en HU-W16
    concurrencia=settings.sqs_concurrencia,   # SQS_CONCURRENCIA
    visibilidad_s=30,                # la de la cola en catalog.tf
    plazo_parada_s=10,
    al_fallar=registrar_fallo,       # opcional
)
parada = asyncio.Event()
tarea = asyncio.create_task(consumidor.ejecutar(parada))
# En el cierre del lifespan: parada.set(); await asyncio.wait_for(tarea, plazo_parada_s + 5)
```

El manejador escribe su efecto con `tx.conexion` (modo a) o devuelve una continuación para el efecto externo (modo b). Las reglas de HU-W16 no cambian: duplicado → commit y borrado; fallo → rollback, sin borrar; `EventoInvalido` → envenenado (termina en la DLQ).

**Pool:** con `concurrencia = N`, el pool debe admitir N conexiones (`DB_POOL_SIZE` ≥ N más las del resto del servicio).

## 3. Regla de `aggregateVersion` (para HU-W21)

`solventa_common.versionado` deja la regla y su prueba. **La proyección `coberturas_activas` y su manejador son de HU-W21 (capbmaster)**; aquí solo está la regla, sin tablas de `pagos-parametricos`.

- `avanza(actual, nueva)`: sin estado previo o con una versión mayor, avanza; con igual o menor, no.
- `leer_version(evento)`: el `aggregateVersion` del sobre (entero ≥ 1); si falta, `EventoInvalido` (el mensaje va a la DLQ, porque sin versión no se puede ordenar).
- `upsert_por_version(tabla, clave, columnas)` y `aplicar_por_version(conexion, sentencia, clave, valores, version)`: la regla en un `INSERT … ON CONFLICT DO UPDATE … WHERE tabla.aggregate_version < EXCLUDED.aggregate_version`, en la transacción del inbox. Devuelve False si el evento estaba atrasado; se confirma igual y el mensaje se borra.

Con esto, una `PolizaCancelada` (versión 2) que llega antes que la `PolizaEmitida` (versión 1) deja la cobertura cancelada. La emisión atrasada ya no la reactiva.

**Dependencia abierta:** hoy `polizas` no publica `aggregateVersion` en `PolizaEmitida` ni existe `PolizaCancelada`. La regla solo tiene efecto cuando el productor publique la versión del agregado. Queda anotado para capbmaster (HU-W21) y para quien sea responsable de `polizas`.

## 4. Auditoría por lotes

`auditoria` ya no es un esqueleto: consume la cola `auditoria` (todos los eventos, D3) con `ConsumidorLotes` (`services/auditoria/src/auditoria/adapters/inbound/sqs/lotes.py`) y escribe **un objeto por lote recibido**.

- **Registro** (`domain/registro.py`): el `RegistroAuditoria` de `diagrama_flujo_estructuras_informacion.puml`: `actor` (el `payload.actorId` si es un identificador; si no, `sistema`), `accion` (= `type`), `resourceId` (= `aggregateId`), `timestamp` (= `occurredAt`) y `previousHash`. Además lleva `eventId`, `correlationId`, `registradoEn`, el **evento tal como llegó** (`evento`, el texto del sobre) y su `hash` = SHA-256 del JSON canónico del registro sin `hash`.
- **Cadena:** cada registro enlaza con el anterior (`previousHash`), también entre lotes. El primero parte de `GENESIS` (64 ceros). `verificar_cadena` detecta un registro alterado, quitado o reordenado.
- **Lote** (`application/registrar_lote.py`), en una transacción: (1) bloquea la cabeza de la cadena (`cadena`, una fila, `FOR UPDATE`), así que las N tareas escriben en serie; (2) inserta los `eventId` en el `inbox` (los que ya estaban son duplicados y no se reescriben); (3) sella los nuevos en orden (`occurredAt`, `eventId`); (4) escribe `cadena/<secuencia 12 dígitos>-<hash>.json` en el bucket con Object Lock, con `If-None-Match: *` (nunca reemplaza); (5) avanza la cabeza. Después del commit borra los mensajes del lote.
- **Fallos:** si S3 o la base fallan, rollback y nada se borra: el lote vuelve tras 60 s. Si el objeto se escribió pero el commit falló, queda un objeto **huérfano**: su `previousHash` enlaza, pero ninguna cabeza apunta a él, y el reintento escribe otro con la misma secuencia y otro hash. `AlmacenS3.leer_cadena(secuencia, hash)` recorre desde la cabeza confirmada hacia atrás y deja fuera los huérfanos.
- **Envenenado** (sin sobre, o sin `aggregateId` u `occurredAt`): no se registra ni se borra, y el resto del lote sigue. Tras 5 recepciones va a `auditoria-dlq`.
- **Base:** `auditoria` pasa a `database = true` (decisión 3 de la revisión del plan). Guarda solo el inbox y la cabeza; el registro es el de S3. Respaldo: `ConsumidoresAuditoria --> RegistroIdempotencia` (`modelo_concurrencia.puml`).
- **Escalado por atraso:** ya escala por la profundidad de su cola (política `backlog` de `infra/apps/autoscaling.tf`). No se tocó: HU-W25 (Santi-MISO) ajusta el escalado por atraso de todos los consumidores. Ritmo esperado: un lote por transacción serializada, unos 200 a 300 lotes/s (2.000 a 3.000 eventos/s) con S3 en la misma región. HU-W26 mide su drenaje aparte del criterio de 10 minutos (D3).

## 5. DLQ visible y reprocesable (H10 y H11)

**Backend** (`pagos-parametricos`, rol `ANALISTA_SINIESTROS`; vía `bff-web`, grupo `analistas-siniestros`):

| Ruta (`pagos-parametricos` / `bff-web`) | Pantalla | Qué hace |
| --- | --- | --- |
| `GET /v1/parametricos/descartes` / `GET /v1/siniestros/parametricos/descartes` | H10 | Hasta 100 mensajes de `parametricos-eventos-dlq` **sin consumirlos**: los recibe con visibilidad de 30 s y los libera. Causa, intentos, póliza referida, resumen por causa, retención de la DLQ (14 días) y `maxIntentos` (5) |
| `GET …/{messageId}` | H11 | Contenido recibido, clave de idempotencia (= `eventId`), causa y verificación de HU-W21 (null mientras no exista) |
| `POST …/reproceso` `{messageIds}` | H10 "Reprocesar seleccionados" y H11 "Reprocesar sin cambios" | Reenvía cada uno a `parametricos-eventos` con el **mismo `eventId`** y el atributo `eventType`, y lo borra de la DLQ |
| `POST …/{messageId}/correccion` `{cambios, motivo?}` | H11 "Corregir y reprocesar" | Cambia campos que ya existen en el `payload` (solo valores simples) y reenvía el cuerpo corregido con el mismo `eventId` |
| `POST …/{messageId}/descarte` `{motivo}` | H11 "Descartar definitivamente" | Lo borra de la DLQ sin reenviar |

- **Causa del fallo:** SQS no la guarda. El consumidor de `parametricos-eventos` la anota con `al_fallar` en `fallos_consumo`, por `MessageId`, que SQS conserva al mover el mensaje a la DLQ. Categorías: `FORMATO_INVALIDO` (envenenado o esquema), `REFERENCIA_INEXISTENTE` (el manejador lanzó `PolizaInexistente` o `CoberturaInexistente`), `ERROR_PROCESAMIENTO` y `DESCONOCIDA`.
- **Auditoría (decisión B2):** cada resolución se guarda en `resoluciones_descarte` y publica `DescarteParametricoResuelto.v1` por el Outbox **en la misma transacción y antes del efecto**. El evento lleva el **original** y, si se corrigió, la **corrección**, y lo recibe solo `auditoria`. Esquema: `contracts/events/DescarteParametricoResuelto.v1.schema.json`.
- **Idempotencia:** repetir la misma acción sobre un mensaje ya resuelto devuelve la resolución registrada; otra acción es 409 `DESCARTE_YA_RESUELTO`. Si el reenvío falla, el mensaje se libera y sigue en la DLQ, y la resolución ya registrada permite completarla. Si el borrado falla después de reenviar, el mensaje queda en las dos colas: el consumidor lo deduplica por `eventId`. Un mensaje tomado por otro analista (oculto por su lectura) es 404.
- **"Pago previo por este evento":** el mismo `eventId` evita un segundo pago aunque se reprocese dos veces (Inbox del consumidor y clave única evento-póliza de HU-W21). La N2 lo prueba.
- **Llamada `bff-web → pagos-parametricos` (decisión B1):** no tiene flecha en los diagramas (`ServiciosWeb` solo llega a `ClientesIdentidad`, `SuscripcionPolizas` y `AtencionSiniestros`). Se aprobó como excepción para HU-W24 y la necesita también H09 (HU-W22). La revisión de la DLQ es la (8) de `modelo_componentes.puml`: "Tras reintentos acotados, el mensaje pasa a revisión". **Pendiente para quien mantiene los diagramas:** agregar `ServiciosWeb --> PagosParametricos`.
- **Discrepancias con los mockups:** H10 dice "tras tres intentos", pero se muestra el `maxReceiveCount` real (5). H10 no tiene casillas de selección, pero "Reprocesar seleccionados" las necesita: se agregaron. "Proporción sobre el total" no se muestra, porque el total de procesados es de H09 (HU-W22).

## 6. Infraestructura

Detalle en [infra/PLATAFORMA.md](../infra/PLATAFORMA.md#hu-w24-eventos-paramétricos-con-particiones): base de `auditoria`, `bff-web.calls` con `pagos-parametricos`, IAM `dlq_operators` (solo la DLQ de `parametricos-eventos` y `SendMessage` a su cola origen) y el paso 13 del humo. La retención de 4 días y las alarmas de atraso y de DLQ ya estaban (HU-S03).

## 7. Reproceso desde el archivo `telemetria/`

`parametricos-eventos` retiene 4 días y su DLQ 14. Un evento que ya salió de las dos se recupera del archivo de originales (`diagrama_estrategia_replicacion_recuperacion.puml`: `Archiver -right-> S3Primary : archiva para reproceso` y "Orquestador de replay: lectura y publicación"). Es un procedimiento de operación, no un servicio: ninguna tarea tiene lectura sobre `telemetria/` (solo `adaptador-telemetria` escribe ahí).

1. Con credenciales de operador, listar los originales del intervalo en `s3://solventa-<amb>-evidencias-<cuenta>/telemetria/` (la clave por fecha la fija HU-W21).
2. Por cada original, armar el sobre `EventoParametricoObservado` **con el mismo `eventId`** con que se publicó. HU-W21 debe derivar ese `eventId` del identificador del evento del proveedor (o guardarlo junto al original); si no, el reproceso no sería idempotente.
3. Publicarlo en el tópico de eventos de negocio con el atributo `eventType = EventoParametricoObservado`. Llega a `parametricos-eventos` (y a `auditoria`); el Inbox y la clave única evento-póliza descartan lo ya procesado.
4. Verificar en la DLQ (H10) que no aparecieron mensajes nuevos.

Publicar en el tópico es una escritura en AWS: se ejecuta solo con aprobación explícita.

## 8. Portal (H10 y H11)

`proyecto-final-2-frontend/web/src/app/features/siniestros/parametricos/descarte/`: `descarte.routes.ts` (ruta `/siniestros/parametricos/descarte` y `/:messageId`, guarda `analistas-siniestros`), `descarte.service.ts`, `descarte.i18n.ts` (es-CO, en-US y pseudo), `descarte.errores.ts`, `descarte.fixtures.ts`/`.testing.ts`, las páginas `cola-descarte` (H10) y `reproceso` (H11) y un layout mínimo (`descarte-layout.component.ts`).

**Lo mínimo para no interferir con HU-W19 a HU-W23 (capbmaster):**
- Una sola ruta nueva en `app.routes.ts`. No se tocan el menú superior ("Siniestros" sigue sin enlace), `inicio()` ni un `siniestros.routes.ts`.
- El layout mínimo tiene "Mi bandeja" y "Sin asignar" como próximamente. Cuando exista el layout de Siniestros: mover `descarte.routes.ts` como hijo de `siniestros/parametricos/descarte`, quitar la ruta de `app.routes.ts` y retirar `descarte-layout.component.ts`.
- ESLint cubre `src/app/features/siniestros` (`eslint.config.mjs` y `npm run lint:siniestros`).
- H09 ("Ver la cola de descarte") debe enlazar a `/siniestros/parametricos/descarte`.

## 9. Para capbmaster (HU-W21 a HU-W23) y Santi-MISO (HU-W25)

- **Consumidor:** sección 2. Para `parametricos-eventos`: `ConsumidorInbox(ReceptorSqs(..., max_mensajes=10), InboxPg(pool), manejador, concurrencia=settings.sqs_concurrencia, visibilidad_s=30, al_fallar=RegistroFallosPg(pool, reloj).al_fallar)`. Sin `al_fallar`, H10 muestra "Causa no registrada".
- **Causas:** para que H10 diga "La póliza referida no existe", el manejador lanza una excepción llamada `PolizaInexistente` o `CoberturaInexistente`. Un evento que no valida contra su esquema lanza `EventoInvalido`.
- **Póliza referida:** H10 la toma de `payload.numeroPoliza`, `polizaNumero`, `polizaId` o `poliza` (`CAMPOS_POLIZA` en `domain/descarte.py`). Ajustar al esquema de `EventoParametricoObservado`.
- **Verificación de H11:** implementar `Verificaciones` (`application/descartes.py`) con `polizaVigente`, `coberturaActiva`, `pagoPrevio`, `umbral` y `valorIndemnizar`, y pasarla a `ColaDescarte` en `armar_cola` de `main.py`.
- **Migraciones de `pagos-parametricos`:** HU-W24 dejó `001_descartes.sql` (`fallos_consumo`, `resoluciones_descarte` y el `outbox`, que HU-W21 comparte) y `migrar()`. HU-W21 sigue en `002_…`.
- **Regla de versión:** sección 3. Falta que `polizas` publique `aggregateVersion` (y `PolizaCancelada`).
- **HU-W25:** `ejecutar(parada)` ya drena: deja de recibir, libera lo no empezado y espera `plazo_parada_s`. La política `backlog` de `auditoria` y `pagos-parametricos` existe; el escalado por atraso es tuyo.

## 10. Humo R5 en `int` (preparado, sin ejecutar)

Paso 13 de `scripts/plataforma/smoke.sh`: publica 5 `EventoParametricoObservado` sintéticos en el tópico y verifica que cada `eventId` aparezca **una vez** en los objetos `cadena/*.json` de auditoría y que la cola `auditoria` drene. Requiere `auditoria` desplegado con su base.

```bash
make infra-plataforma ENV=int                                     # base y secreto de auditoria (apply: pedir confirmación)
make infra-bases ENV=int                                          # crea la base en RDS
make infra-microservicios ENV=int SERVICES=auditoria,pagos-parametricos,bff-web
make infra-probar ENV=int SMOKE_DLQ=0                             # pasos 1-13; SMOKE_W24_EVENTOS=5
```

Luego, en el portal con un usuario de `analistas-siniestros`: un mensaje envenenado enviado a `solventa-int-parametricos-eventos-dlq` aparece en H10 y se descarta en H11.

## 11. Pendientes

- La flecha `ServiciosWeb --> PagosParametricos` en `modelo_componentes.puml` y `modelo_despliegue.puml` (decisión B1).
- `aggregateVersion` y `PolizaCancelada` en `polizas`; la proyección `coberturas_activas` y la verificación de H11 (HU-W21).
- Pacto del portal (`web-bff-web.json`) para H10 y H11: el contrato del BFF se cubre hoy con la igualdad de DTO frente a `pagos-parametricos.yaml` (`test_openapi.py`). Agregarlo junto con el de H09 (HU-W22).
- Las E2E solo corrieron en Chromium (Firefox y WebKit no están instalados en esta Mac).
## 12. Pruebas

| Nivel | Archivo | Qué cubre |
| --- | --- | --- |
| N1 | `libs/solventa-common/tests/unit/test_hu_w24_consumidor.py` | Contrapresión (huecos libres, tope de 10, máximo en mano), borrado después del commit, latido (también para los que esperan turno), parada (libera lo que espera turno, plazo vencido → rollback y liberación, mensaje tardío), causa del fallo, `ReceptorSqs` |
| N1 | `libs/solventa-common/tests/unit/test_hu_w24_versionado.py` | `avanza` con todas las permutaciones y duplicados, `leer_version` y la sentencia |
| N2 | `libs/solventa-common/tests/module/test_hu_w24_particiones.py` | LocalStack (SNS → colas filtradas + DLQ) y PostgreSQL: duplicados en dos tareas, cancelación antes de emisión, orden aleatorio con duplicados, visibilidad vencida sin latido y con latido, envenenado a la DLQ con su causa por `MessageId`, grupo caído mientras otro procesa, contrapresión con dos tareas y parada que libera |

| N1 | `services/auditoria/tests/unit/test_registro.py`, `test_lotes.py`, `test_ciclo.py` | Contrato `RegistroAuditoria`, cadena (alterar, quitar o reordenar la rompe), un objeto por lote, duplicados, rollback si S3 falla, envenenados, borrado tras el commit, clave e `If-None-Match`, arranque y parada |
| N2 | `services/auditoria/tests/module/test_auditoria_lotes.py` | LocalStack (SQS + S3 con Object Lock) y PostgreSQL: avalancha de 110 mensajes (95 eventos y 15 duplicados) con 3 tareas, en una cadena sin huecos ni repetidos que verifica; objeto no reemplazable; huérfano fuera de la cadena; envenenado a la DLQ |
| N1 | `services/pagos-parametricos/tests/unit/test_descarte_dominio.py`, `test_descartes.py` | Causa, póliza, corrección; listar sin consumir, reprocesar, corregir, descartar, reintento tras fallo, idempotencia, 404 y 409; API con actor y rol |
| N2 | `services/pagos-parametricos/tests/module/test_descarte_reproceso.py` | El envenenado llega a la DLQ con su causa; corregir y reprocesar paga una vez; reprocesar otra vez el mismo `eventId` no paga de nuevo; el Outbox valida contra el esquema; descartar es idempotente |
| Contrato | `services/pagos-parametricos/tests/contract/`, `services/bff-web/tests/contract/test_openapi.py` | OpenAPI publicado, esquema de `DescarteParametricoResuelto.v1` y DTO de `bff-web` iguales a los de `pagos-parametricos` |
| N1 | `services/bff-web/tests/unit/test_descartes.py` | Grupo `analistas-siniestros`, actor y correlación hacia `pagos-parametricos`, 4xx de la lista blanca y falla cerrada (503) |
| IaC | `infra/apps/tests/apps.tftest.hcl` (run `sprint3`), `infra/platform/tests/platform.tftest.hcl` | IAM de la DLQ solo para `pagos-parametricos`, `PAGOS_PARAMETRICOS_URL`, base y escalado de `auditoria` |
| Web | `features/siniestros/parametricos/descarte/**/*.spec.ts` (Jasmine) y `e2e/r5-descarte.spec.ts` (Playwright con axe) | H10 y H11 en es-CO y en-US, selección, corrección, descarte, vacío, 503, 404, acceso por grupo y axe sin violaciones críticas ni serias |

```bash
make test-unit                                          # incluye libs/*/tests/unit
TESTCONTAINERS_RYUK_DISABLED=true make test-module      # libs/*/tests/module
TESTCONTAINERS_RYUK_DISABLED=true make test-module SERVICE=auditoria
TESTCONTAINERS_RYUK_DISABLED=true make test-module SERVICE=pagos-parametricos
make test-contract SERVICE=pagos-parametricos && make test-contract SERVICE=bff-web
TF_DATA_DIR=$(mktemp -d) terraform -chdir=infra/apps test   # y infra/platform
# Portal (proyecto-final-2-frontend/web):
npx ng test --watch=false --browsers=ChromeHeadless --include='src/app/features/siniestros/**/*.spec.ts'
npx playwright test e2e/r5-descarte.spec.ts && npm run lint:siniestros
```
