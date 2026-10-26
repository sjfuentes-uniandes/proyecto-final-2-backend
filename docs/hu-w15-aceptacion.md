# HU-W15 · Registro de aceptación

Issue: [sjfuentes-uniandes/proyecto-final#57](https://github.com/sjfuentes-uniandes/proyecto-final/issues/57). Recorrido R3. Pantalla F01 (portal).

Un asesor registra, en nombre del cliente identificado, la aceptación de una oferta con decisión `APROBADA` y vigente. La aceptación queda como **evidencia inmutable** en `polizas` y publica `OfertaAceptada.v1` por Transactional Outbox, en la misma transacción, para habilitar el recaudo (HU-W16).

## 1. Diseño en una página

- **Sujeto:** la cotización (DS2-15). A lo sumo **una aceptación por cotización** (única por `cotizacion_id`) y por `Idempotency-Key`.
- **Sin llamadas a otros servicios:** la validación usa la instantánea de la oferta que guardó la decisión de HU-W13 (`vigenteHasta`, prima, `huellaCalculo`; DS2-10) y el reloj de `polizas`.
- **Quién acepta:** un usuario del grupo `asesores` (DS2-09). La evidencia guarda `actorId` (el asesor), `clienteId` y `canal = PORTAL_ASESOR`.
- **Versión del texto (DS2-24):** la solicitud trae `versionDeclaraciones`; debe ser la vigente de `polizas` (`SOLVENTA_VERSION_DECLARACIONES`, por omisión `2026.10.1`).
- **Código:** dominio puro en `services/polizas/src/polizas/domain/aceptacion/` (`validar_aceptacion`, `huella_evidencia`); caso de uso `application/aceptacion/registrar.py`; persistencia `adapters/outbound/aceptaciones_pg.py` y migración `002_aceptaciones.sql` (con su `down/`); rutas `adapters/inbound/http/aceptaciones.py`. En `bff-web`, `adapters/inbound/aceptaciones.py` (DTO copiados de `polizas.yaml`) y `application/aceptacion.py` (falla cerrada).

### 1.1 Rutas

| Ruta (`polizas` y `bff-web`) | Quién | Respuestas |
| --- | --- | --- |
| `POST /v1/cotizaciones/{cotizacionId}/aceptacion` con `{decisionId, versionDeclaraciones, declaraciones}` e `Idempotency-Key` | `ASESOR` (`bff-web`: grupo `asesores`) | 201 `Aceptacion`; 200 la misma con la misma clave; 404, 409 y 422 de la tabla 1.2 |
| `GET /v1/cotizaciones/{cotizacionId}/aceptacion` | `ASESOR` o `SUSCRIPTOR` en `polizas`; grupo `asesores` en `bff-web` | 200 `Aceptacion`; 404 `ACEPTACION_NO_ENCONTRADA` |

`Aceptacion`: `id`, `cotizacionId`, `ofertaId` (null antes de HU-W04), `decisionId`, `clienteId`, `huellaCalculo`, `versionDeclaraciones`, `declaraciones`, `canal`, `actorId`, `aceptadaEn`, `huellaEvidencia`, `recaudoHabilitado` (siempre `true`) y `siguientePaso` (`FIRMA`). `declaraciones`: `leidoCondiciones` e `informacionVeraz` (obligatorias), `debitoAutomatico` y `avisosElectronicos` (se guardan con su valor).

En `bff-web` pasan tal cual los 4xx del contrato; cualquier 5xx de `polizas`, otro código, un timeout o un cuerpo fuera del DTO es 503 `SERVICIO_NO_DISPONIBLE`.

### 1.2 Tabla de validación

El caso de uso aplica las condiciones en este orden; la primera que falla decide y no se escribe nada:

| # | Condición | Respuesta |
| - | --- | --- |
| 1 | La `Idempotency-Key` ya registró una aceptación | Mismo cuerpo: **200** con la existente (aunque la oferta ya haya vencido). Otro cuerpo: **422** `IDEMPOTENCIA_CONFLICTO` |
| 2 | La cotización no tiene decisión, o `decisionId` no es la suya | **404** `DECISION_NO_ENCONTRADA` (no se distingue cuál) |
| 3 | La cotización ya tiene aceptación con otra clave | **409** `OFERTA_YA_ACEPTADA`, `detalles.aceptacionId` |
| 4 | `versionDeclaraciones` ≠ la vigente | **422** `TEXTO_NO_VIGENTE` |
| 5 | La decisión es `RECHAZADA` o `REVISION_REQUERIDA` | **409** `DECISION_NO_APROBADA` |
| 6 | `ahora ≥ vigenteHasta` (el instante exacto ya es vencido) | **409** `OFERTA_VENCIDA`, `detalles.accion = NUEVA_COTIZACION` |
| 7 | Falta `leidoCondiciones` o `informacionVeraz` | **422** `DECLARACIONES_INCOMPLETAS`, `detalles.campos` |
| — | Todo bien | **201**: inserta la aceptación y la fila `OfertaAceptada` en el `outbox` en una transacción |

Los pasos 4 a 7 son la función pura `validar_aceptacion` (precedencia texto → decisión → vigencia → declaraciones). La carrera entre solicitudes la resuelve la unicidad de la tabla: si gana la misma clave, la perdedora responde 200 con la ganadora; si gana otra, 409 `OFERTA_YA_ACEPTADA`.

### 1.3 Evidencia inmutable

La tabla `aceptaciones` tiene un disparador que rechaza `UPDATE` y `DELETE`. `huellaEvidencia` es SHA-256 del JSON canónico (claves ordenadas, sin espacios, instantes UTC con `Z`) de: cotización, oferta, decisión, `huellaCalculo`, `vigenteHasta`, `versionDeclaraciones`, declaraciones, cliente, actor, canal y `aceptadaEn`. Todo está en la fila, así que se recalcula igual desde la base.

## 2. Para HU-W16: cómo consumir `OfertaAceptada`

- **Esquema:** `contracts/events/OfertaAceptada.v1.schema.json` (generado desde `polizas/adapters/outbound/eventos.py`). Sobre común (`eventId`, `aggregateId` = `aceptacionId`, `type = "OfertaAceptada"`, `version = 1`, `occurredAt`, `correlationId`, `payload`).
- **Payload:** `aceptacionId`, `cotizacionId`, `ofertaId` (puede ser null), `decisionId`, `clienteId`, `productoId`, `prima {valor (texto decimal), moneda, periodicidad}`, `claveRecaudo`, `huellaCalculo`, `vigenteHasta`, `versionDeclaraciones`, `aceptadaEn`, `actorId` y `canal`. Sin nombre, documento, edad, suma asegurada, declaraciones ni datos de medios de pago: `dinero` los pide por su propio canal cuando cobre.
- **Ruta:** el relay del Outbox (EXT-04, todavía no existe) publica en SNS con el atributo `eventType = OfertaAceptada`, que filtra la cola `dinero-aceptaciones`. Mientras no exista, las filas quedan en el `outbox` de `polizas` con `published_at` nulo.
- **Idempotencia del consumidor:** registrar en el Inbox el `eventId` (regla 4). Además, usar **`claveRecaudo` como clave única del cobro**: es igual a `aceptacionId` y no cambia aunque el relay publique el mismo evento dos veces o lo reenvíe con otro `eventId`. Una aceptación produce a lo sumo un recaudo.
- **Monto:** `prima.valor` es texto decimal (DS2-16); conviértelo a `Decimal`, nunca a `float`. La periodicidad del recaudo es `prima.periodicidad`.
- **Correlación:** propagar `correlationId` del sobre en las llamadas a `adaptador-pagos`, para que la traza de R3 quede de punta a punta.
- **Vigencia:** el evento ya trae `vigenteHasta`, pero `dinero` no debe revalidarla: la aceptación registrada es la autorización del cobro.

## 3. Recorrido local

### 3.1 Levantar el ambiente

```bash
make dev-up
```

`polizas` aplica `002_aceptaciones.sql` al arrancar. `bff-web` queda en el 8080 (si está ocupado, ver R-33 en la bitácora del sprint).

### 3.2 Aceptar una aprobada

Con una decisión `APROBADA` de S01 (cómo crearla: `docs/hu-w13-suscripcion.md` §2.2), por `bff-web` como asesor (en local `bff-web` confía en los encabezados `X-Authenticated-*`):

```bash
curl -s -X POST localhost:8080/v1/cotizaciones/<cotizacionId>/aceptacion \
  -H 'X-Authenticated-Sub: sub-asesores-local' -H 'X-Authenticated-Groups: [asesores]' \
  -H "Idempotency-Key: $(uuidgen)" -H 'Content-Type: application/json' \
  -d '{"decisionId":"<decisionId>","versionDeclaraciones":"2026.10.1","declaraciones":{"leidoCondiciones":true,"informacionVeraz":true,"debitoAutomatico":true,"avisosElectronicos":false}}'
```

Repetir con la misma `Idempotency-Key` responde 200 con el mismo cuerpo; con otra, 409 `OFERTA_YA_ACEPTADA`.

### 3.3 La aceptación y su fila de `outbox`

```bash
docker compose exec postgres psql -U solventa -d polizas -c \
  "SELECT a.id, a.cotizacion_id, a.canal, a.version_declaraciones, a.huella_evidencia, o.event_type, o.correlation_id = a.correlation_id AS misma_correlacion
     FROM aceptaciones a JOIN outbox o ON o.aggregate_id = a.id"
docker compose exec postgres psql -U solventa -d polizas -c "UPDATE aceptaciones SET canal = canal"
```

Esperado: una fila `OfertaAceptada` por aceptación con el mismo `correlation_id`, y el `UPDATE` rechazado (`aceptaciones es inmutable`).

### 3.4 Oferta vencida

La oferta vale 7 días, así que en local no vence sola. Como en HU-W14 (§3.3 de `docs/hu-w14-comunicacion.md`), se levanta una segunda instancia de `polizas` con el reloj adelantado 8 días sobre la misma base, sin cambiar código, y se le envía la aceptación de una aprobada **todavía no aceptada**:

```bash
SOLVENTA_DATABASE_URL=postgresql://solventa:solventa@localhost:5432/polizas \
uv run python -c "
from datetime import timedelta
import uvicorn
from polizas.adapters.outbound import reloj
real = reloj.RelojSistema.ahora
reloj.RelojSistema.ahora = lambda self: real(self) + timedelta(days=8)
from polizas.main import crear_app
uvicorn.run(crear_app(), host='127.0.0.1', port=18088)"
```

```bash
curl -s -X POST localhost:18088/v1/cotizaciones/<cotizacionId>/aceptacion \
  -H 'X-Actor-Id: sub-asesor' -H 'X-Actor-Rol: ASESOR' -H "Idempotency-Key: $(uuidgen)" \
  -H 'Content-Type: application/json' -d '<el mismo cuerpo de 3.2>'
```

Esperado: 409 `OFERTA_VENCIDA` con `detalles.accion = NUEVA_COTIZACION` y ninguna fila nueva en `aceptaciones` ni en `outbox`.

### 3.5 Resultado del recorrido

2026-10-24, local, `make dev-up` con las imágenes reconstruidas desde la rama `feat/hu-w15-registro-aceptacion` (`bff-web` en el 8080). Todo por `bff-web` como asesor, salvo la vencida (§3.4):

| Caso | Respuesta |
| --- | --- |
| S01 aprobada (`DEC-2026-000028`) | 201, `recaudoHabilitado = true`, `siguientePaso = FIRMA` |
| Misma clave | 200 con el mismo cuerpo |
| Otra clave | 409 `OFERTA_YA_ACEPTADA`, `detalles.aceptacionId` = la registrada |
| `GET` de la aceptación | 200 con el mismo cuerpo |
| S02 rechazada (68 años) | 409 `DECISION_NO_APROBADA` |
| S06 en revisión (620.000.000) | 409 `DECISION_NO_APROBADA` |
| Otra S01 sin `leidoCondiciones` | 422 `DECLARACIONES_INCOMPLETAS`, `detalles.campos = ["leidoCondiciones"]` |
| Otra S01 con `versionDeclaraciones = 2026.09.1` | 422 `TEXTO_NO_VIGENTE` |
| `decisionId` ajeno | 404 `DECISION_NO_ENCONTRADA` |
| Grupo `suscriptores` | 403 `ROL_NO_AUTORIZADO` |
| Otra S01 con el reloj +8 días (§3.4) | 409 `OFERTA_VENCIDA`, `detalles.accion = NUEVA_COTIZACION`; filas antes y después: 1 aceptación y 1 `OfertaAceptada` |

En la base quedó una aceptación con su fila `OfertaAceptada` (mismo `correlation_id`, `claveRecaudo` = `aceptacionId`, prima `{"valor": "64638", "moneda": "COP", "periodicidad": "MENSUAL"}`, `published_at` nulo por EXT-04), junto a la `DecisionSuscripcionRegistrada` de su decisión. `UPDATE` y `DELETE` sobre `aceptaciones` fallan con «aceptaciones es inmutable». Los logs de `polizas` y `bff-web` de la aceptación comparten el `X-Correlation-Id` y no contienen la suma asegurada, la fecha de nacimiento, las declaraciones ni nombres.

**Cierre (fase C, mismo día):** el portal local (arnés de R-50 y R-82 de la bitácora del sprint) recorrió R3 contra `make dev-up`: C04 → «Continuar con la suscripción» → E02 → «Continuar a la aceptación» → F01 → confirmación, en es-CO y en-US, con una sola aceptación aunque se pulsó dos veces; el rechazo (68 años) y la revisión (620.000.000) sin opción de aceptar y con 409 `DECISION_NO_APROBADA` al forzar el `POST`; `cotizacion` detenido → 503 `DEPENDENCIA_NO_DISPONIBLE` en 243 ms sin decisión y la misma clave decide al reanudarlo; y la oferta vencida (reloj +8 días) con el estado tipo C08 en el portal. `bff-web` verifica el pacto ampliado del portal (`contracts/pact/web-bff-web.json`, 10 interacciones) en `tests/contract/test_pact_web.py`. Evidencia en `docs/sprint_2/evidencias/hu-w15/` del espacio de trabajo.

## 4. Pruebas

| ID | Nivel | Dónde |
| --- | --- | --- |
| UT-W15-01 | Dominio: precedencia, vigencia en el límite, declaraciones, versión del texto, huella | `services/polizas/tests/unit/aceptacion/test_dominio.py` |
| UT-W15-02 | Caso de uso: idempotencia, conflicto, ya aceptada, decisión que no coincide, carrera, operación medida | `services/polizas/tests/unit/aceptacion/test_registrar_aceptacion.py` |
| — | API y persistencia de `polizas` con dobles | `services/polizas/tests/unit/aceptacion/test_api.py`, `test_persistencia.py` |
| MT-W15-01 | PostgreSQL efímero: transacción con el `outbox`, vencida, 20 concurrentes, rechazos, inmutabilidad, logs sin PII, migraciones | `services/polizas/tests/module/test_aceptacion_w15.py` |
| CT-W15-01 | Esquema del evento, `test_openapi` y Schemathesis de `polizas`; DTO de `bff-web` iguales a `polizas.yaml` | `services/polizas/tests/contract/`, `services/bff-web/tests/contract/test_openapi.py` |
| — | `bff-web`: solo `asesores`, errores del contrato y falla cerrada | `services/bff-web/tests/unit/test_aceptacion.py` |
| N3 R3 | Cotización → decisión → aceptación y rechazo → 409, por `/web` | `tests/e2e/test_hu_w13_w15_r3.py` (en `int` cuando existan EXT-01 y EXT-02) |
