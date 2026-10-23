# HU-W13 · Suscripciones automáticas

> Como cliente con una oferta quisiera que Solventa aplique las reglas automáticas de suscripción a mi solicitud para obtener una decisión sin revisión manual. (Issue #55)

Este documento recoge el diseño de la historia en el backend y cómo verificarla en local. Los contratos están en `contracts/` (`openapi/polizas.yaml`, `openapi/catalogo.yaml`, `openapi/cotizacion.yaml`, `openapi/bff-web.yaml`, `modelos/reglas-suscripcion.v1.schema.json`, `events/DecisionSuscripcionRegistrada.v1.schema.json` y `datos-sinteticos/cotizaciones-suscripcion.json`). La decisión de arquitectura propuesta está en [ADR 0012](adr/0012-suscripcion-antes-de-aceptacion.md).

## 1. Diseño en una página

| Tema | Decisión |
| --- | --- |
| Recorrido | `bff-web` → `polizas` (`POST /v1/suscripciones`) → `cotizacion` (insumos de la oferta) y luego `catalogo` (reglas vigentes) → motor → decisión y evento `DecisionSuscripcionRegistrada.v1` en el outbox, en una transacción |
| Reglas | Configuración versionada en `catalogo` (`reglas_suscripcion`, inmutable, semilla `2026.10.1`): SUS-001 edad 18–65 (excluyente), SUS-004 suma ≤ 500.000.000 (revisión), SUS-007 nivel ≠ ALTO (revisión), SUS-011 confianza ≥ 0,70 (revisión), SUS-015 sin coincidencias en listas (excluyente, **reservada**), SUS-019 plazo ≤ 240 meses (excluyente) |
| Motor (`polizas/domain/suscripcion/motor.py`) | Puro y determinista. Excluyente incumplida → `RECHAZADA` (determina la primera por orden); revisión incumplida o cualquier regla no evaluable → `REVISION_REQUERIDA`; todo cumple → `APROBADA`. Sin dato nunca rechaza ni aprueba |
| Precondiciones | Identidad `RECHAZADO` → 403 `IDENTIDAD_NO_VERIFICADA`; `PENDIENTE` → 409 `IDENTIDAD_PENDIENTE`; oferta vencida (`ahora ≥ vigenteHasta`) → 409 `OFERTA_VENCIDA`. Ninguna crea decisión |
| Idempotencia | `Idempotency-Key` obligatoria. Una sola decisión por cotización (única por `cotizacion_id`, `idempotency_key` y `codigo`, con `ON CONFLICT DO NOTHING`): repetir o usar otra clave devuelve la decisión existente con 200; la misma clave con otra cotización es 422 `IDEMPOTENCIA_CONFLICTO` |
| Dependencias | `cotizacion` 600 ms y `catalogo` 300 ms, en serie (las reglas dependen del producto), con caché en memoria de las reglas (60 s vigentes, 24 h por versión). Falla cerrada: 503 `DEPENDENCIA_NO_DISPONIBLE`, sin decisión. Ninguna conexión de base abierta mientras se espera |
| Reglas reservadas (DS2-07) | `polizas` oculta `valorCaso` al rol `ASESOR`; `bff-web` elige el rol por grupo (`suscriptores` → `SUSCRIPTOR`) y vuelve a filtrar para los asesores |
| Reconstrucción | `GET /v1/suscripciones/{id}/reconstruccion` pide a `catalogo` la versión guardada, comprueba la huella de las reglas y de los insumos y reevalúa |
| Portal (`bff-web`) | `asesores`: `POST /v1/suscripciones`, `GET /v1/suscripciones?cotizacionId=`, `GET /v1/suscripciones/{id}`. `suscriptores`: `GET /v1/suscripciones/{id}`, `GET /v1/bandeja-suscripcion` y `.../resumen`. Las vistas agregan `clienteNombre` desde `clientes` (null si no está disponible) |

## 2. Recorrido local

### 2.1 Levantar el ambiente

```bash
docker compose down -v   # una sola vez: el script de Postgres crea la base polizas solo con el volumen vacío
make dev-up
```

- `polizas` queda en el puerto **8088**; `bff-web` en el 8080 (sin API Gateway: confía en `X-Authenticated-Sub` y `X-Authenticated-Groups`).
- `clientes` siembra C01–C19 al arrancar (`CLIENTES_SEMILLA_SINTETICA`, solo `local` e `int`) y `catalogo` publica la semilla de reglas `2026.10.1` en su migración. No hay `POST` de reglas (DS2-18).
- Si el 8080 (u otro puerto) está ocupado en tu equipo, publica `bff-web` en otro puerto con un archivo propio que no se versiona, por ejemplo `services: {bff-web: {ports: !override ["18080:8080"]}}`, y `docker compose -f compose.yaml -f ese-archivo.yaml up -d`.

### 2.2 Sembrar las cotizaciones y pedir las decisiones

`scripts/dev/recorrido_suscripcion.py` recorre los casos de `contracts/datos-sinteticos/cotizaciones-suscripcion.json` que se reproducen en local: calcula el perfil y la cotización como asesor, pide la decisión y la compara con la esperada; luego muestra la bandeja y el resumen como suscriptor y reconstruye cada decisión.

```bash
uv run python scripts/dev/recorrido_suscripcion.py
```

(Con `BFF_URL=http://localhost:18080` si `bff-web` no está en el 8080.) Para un caso a mano, como asesor:

```bash
curl -s -X POST localhost:8080/v1/suscripciones -H 'X-Authenticated-Sub: sub-asesor' -H 'X-Authenticated-Groups: [asesores]' -H "Idempotency-Key: $(uuidgen)" -H 'Content-Type: application/json' -d '{"cotizacionId": "<id de la cotización>"}'
```

### 2.3 Oferta vencida

Arranca `cotizacion` con `SOLVENTA_OFERTA_VIGENCIA_HORAS=0` (toda cotización nace vencida), cotiza y pide la suscripción: 409 `OFERTA_VENCIDA` con `detalles.accion = NUEVA_COTIZACION` y sin fila en `decisiones_suscripcion`.

### 2.4 Reconstruir una decisión

```bash
curl -s localhost:8088/v1/suscripciones/<decisionId>/reconstruccion -H 'X-Actor-Id: sub-suscriptor' -H 'X-Actor-Rol: SUSCRIPTOR'
```

### 2.5 Resultado del recorrido (2026-10-22, local)

| Caso | Resultado | Regla determinante o reglas no cumplidas |
| --- | --- | --- |
| S01 aprobada (C01) | 201 `APROBADA` | — (seis reglas `CUMPLE`) |
| S02 68 años (C04) | 201 `RECHAZADA` | SUS-001 |
| S03 plazo 300 meses (C02) | 201 `RECHAZADA` | SUS-019 |
| S04 listas restrictivas (C14) | 201 `RECHAZADA` | SUS-015 (y SUS-007 no cumple) |
| S06 suma 620.000.000 (C15) | 201 `REVISION_REQUERIDA` | SUS-004 |
| S07 perfil insuficiente (C03) | 201 `REVISION_REQUERIDA` | SUS-007 no evaluable |
| S08 sin perfil (C06) | 201 `REVISION_REQUERIDA` | SUS-007, SUS-011 y SUS-015 no evaluables |
| S10 identidad rechazada (C11) | 403 `IDENTIDAD_NO_VERIFICADA` | sin decisión |
| S11 identidad pendiente (C12) | 409 `IDENTIDAD_PENDIENTE` | sin decisión |

La bandeja mostró los tres casos en revisión, el resumen 4 resueltas y 3 en revisión, y las siete decisiones se reconstruyeron con `coincide = true`. Los logs de `polizas`, `cotizacion` y `bff-web` no contienen fechas de nacimiento, sumas ni nombres; cada respuesta trae `X-Correlation-Id` y se miden las operaciones de [contracts/operaciones.md](../contracts/operaciones.md) (`suscripcion.evaluar`, `.dependencia.*`, `.bandeja`, `.resumen`, `.reconstruir`, `.solicitar`, `cotizacion.insumos_suscripcion`, `reglas_suscripcion.consultar`).

**Límite conocido:** `clienteNombre` sale `null` en local y en `int`: `GET /v1/clientes/{id}` de `clientes` no expone `nombreCompleto` (lo guarda, pero `ClienteDto` no lo publica). La vista no se rompe; hace falta un cambio aditivo en `clientes` (por acordar con su dueño).

## 3. Pruebas

| Nivel | Dónde | Qué |
| --- | --- | --- |
| N1 `polizas` | `services/polizas/tests/unit/` | Tabla de decisión parametrizada (cada regla × cumple / no cumple / no evaluable), límites (18/65, 500.000.000, 0,70, 240), vigencia en −1 µs / exacto / +1 µs, caso de uso (repetición, conflicto, decisión existente, precondiciones, dependencias caídas y lentas, carreras), adaptadores, persistencia y API (UT-W13-01 a 03, SEC-W13-01) |
| N1 `catalogo`, `cotizacion`, `bff-web` | `tests/unit/suscripcion/` de cada servicio | Validación y huella de las reglas (UT-W13-04), insumos y vigencia (DS2-20), filtrado DS2-07, autorización por grupo y nombre con falla de `clientes` (UT-W13-05) |
| N2 | `services/polizas/tests/module/test_suscripcion_w13.py`, `services/catalogo/tests/module/test_reglas_suscripcion.py`, `services/cotizacion/tests/module/test_suscripcion_w13.py` | Postgres efímero: tres resultados con su evento, 20 POST concurrentes → una fila de decisión y una de outbox, 503 en < 1 s, inmutabilidad, reconstrucción, migraciones adelante y atrás (MT-W13-01 a 03) |
| Contrato | `tests/contract/` de `polizas`, `catalogo`, `cotizacion` y `bff-web` | OpenAPI = app, Schemathesis, esquemas de reglas y evento, DTO de `bff-web` = `polizas.yaml`, pacto `polizas-cotizacion.json` verificado por `cotizacion` (CT-W13-01 y 02) |

## Vigencia de la oferta (para HU-W04)

HU-W13 necesita saber si una oferta está vigente (la suscripción no decide sobre una oferta vencida y responde `409 OFERTA_VENCIDA`). Como HU-W04 aún no define su entidad oferta, W13 implementó una vigencia **provisional y parametrizada** sobre la cotización inmutable de HU-W03 (decisión DS2-20 del plan del Sprint 2).

| Qué | Dónde | Detalle |
| --- | --- | --- |
| Función pura | `services/cotizacion/src/cotizacion/domain/cotizacion/vigencia.py` | `vigente_hasta(calculado_en, vigencia_horas) = calculado_en + vigencia_horas` y `oferta_vigente(hasta, ahora) = ahora < hasta`. Sin reloj ni E/S; probada en los límites (−1 µs vigente, exacto y +1 µs vencida) y con otros valores (`tests/unit/cotizacion/test_vigencia.py`) |
| Parámetro | `services/cotizacion/src/cotizacion/config.py`, `oferta_vigencia_horas` | Variable `SOLVENTA_OFERTA_VIGENCIA_HORAS`, **96 h por omisión**, configurable por ambiente; `0` hace que toda cotización nazca vencida (útil para probar `OFERTA_VENCIDA` en local) |
| Cálculo | Al leer | Sin migración: `calculado_en` ya está guardado y la fila no cambia, así que el resultado es determinista para un mismo parámetro. Cambiar el parámetro cambia `vigenteHasta` de las cotizaciones existentes (no hay instantánea en `cotizacion`) |
| Contratos que la exponen | `contracts/openapi/cotizacion.yaml` y `contracts/openapi/bff-web.yaml` | Campo **opcional** `vigenteHasta` (instante UTC) de `CotizacionRespuesta` (`POST` y `GET /v1/cotizaciones/...`), y campo obligatorio `vigenteHasta` de `InsumosSuscripcion` (`GET /v1/cotizaciones/{cotizacionId}/insumos-suscripcion`) |
| Quién la usa | `polizas` | Lee `vigenteHasta` de los insumos, decide con su propio reloj (`ahora < vigenteHasta`, `polizas/domain/suscripcion/vigencia.py`) y la guarda en la instantánea de la decisión (DS2-10); la aceptación de HU-W15 validará contra esa instantánea, sin volver a llamar a `cotizacion` |

> **Hecho en HU-W04** ([docs/hu-w04-oferta.md](hu-w04-oferta.md#7-traspaso-de-hu-w13)): la vigencia sale de las reglas de prima (`oferta.vigenciaMinutos`, 7 días en 2026.10.1) y se guarda con la oferta; `ofertaId` y `codigo` ya se llenan; `vigenteHasta` conserva nombre y significado; el pacto `polizas-cotizacion` sigue verde. `oferta_vigencia_horas` se conserva como respaldo para reglas sin condiciones de oferta y cotizaciones anteriores a la migración 004.

**Cómo reemplazarla en HU-W04 sin romper `polizas`:**

1. Si W04 crea su entidad oferta con su propia vigencia, calcula `vigenteHasta` con ella en `ConsultarInsumos` (`application/suscripcion/insumos.py`) y en `respuesta_de` (`adapters/inbound/http/cotizaciones.py`), en lugar de `vigente_hasta(...)`. Puede reutilizar `oferta_vigente` para su propio criterio.
2. Conserva **el nombre y el significado** de `vigenteHasta` en ambos contratos: instante UTC y vigencia semiabierta (`ahora < vigenteHasta`). Si cambia el significado, es un cambio incompatible para `polizas` y para el portal.
3. Si W04 crea un identificador propio de la oferta, llena `ofertaId` (y `codigo`, el código legible, CF-S2-10) en `InsumosSuscripcion`: ya existen y hoy van en `null`. `polizas` los guarda y los propaga sin cambios de código (`DecisionSuscripcion.ofertaId` y `cotizacionCodigo`).
4. Mantén en verde el pacto `contracts/pact/polizas-cotizacion.json` (`make test-contract SERVICE=polizas` lo genera y `make test-contract SERVICE=cotizacion` lo verifica) y `tests/contract/test_openapi.py` de `cotizacion` y `bff-web`.
5. Cuando W04 deje de usar el parámetro, retira `oferta_vigencia_horas` de `config.py` y avisa al dueño de `infra/` si se configuró por ambiente.
