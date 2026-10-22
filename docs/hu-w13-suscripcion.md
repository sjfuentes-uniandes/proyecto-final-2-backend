# HU-W13 · Suscripciones automáticas

> Como cliente con una oferta quisiera que Solventa aplique las reglas automáticas de suscripción a mi solicitud para obtener una decisión sin revisión manual. (Issue #55)

Este documento recoge el diseño de la historia en el backend y cómo verificarla en local. Los contratos están en `contracts/` (`openapi/polizas.yaml`, `openapi/catalogo.yaml`, `openapi/cotizacion.yaml`, `openapi/bff-web.yaml`, `modelos/reglas-suscripcion.v1.schema.json`, `events/DecisionSuscripcionRegistrada.v1.schema.json` y `datos-sinteticos/cotizaciones-suscripcion.json`). La decisión de arquitectura propuesta está en [ADR 0012](adr/0012-suscripcion-antes-de-aceptacion.md).

## Vigencia de la oferta (para HU-W04)

HU-W13 necesita saber si una oferta está vigente (la suscripción no decide sobre una oferta vencida y responde `409 OFERTA_VENCIDA`). Como HU-W04 aún no define su entidad oferta, W13 implementó una vigencia **provisional y parametrizada** sobre la cotización inmutable de HU-W03 (decisión DS2-20 del plan del Sprint 2).

| Qué | Dónde | Detalle |
| --- | --- | --- |
| Función pura | `services/cotizacion/src/cotizacion/domain/cotizacion/vigencia.py` | `vigente_hasta(calculado_en, vigencia_horas) = calculado_en + vigencia_horas` y `oferta_vigente(hasta, ahora) = ahora < hasta`. Sin reloj ni E/S; probada en los límites (−1 µs vigente, exacto y +1 µs vencida) y con otros valores (`tests/unit/cotizacion/test_vigencia.py`) |
| Parámetro | `services/cotizacion/src/cotizacion/config.py`, `oferta_vigencia_horas` | Variable `SOLVENTA_OFERTA_VIGENCIA_HORAS`, **96 h por omisión**, configurable por ambiente; `0` hace que toda cotización nazca vencida (útil para probar `OFERTA_VENCIDA` en local) |
| Cálculo | Al leer | Sin migración: `calculado_en` ya está guardado y la fila no cambia, así que el resultado es determinista para un mismo parámetro. Cambiar el parámetro cambia `vigenteHasta` de las cotizaciones existentes (no hay instantánea en `cotizacion`) |
| Contratos que la exponen | `contracts/openapi/cotizacion.yaml` y `contracts/openapi/bff-web.yaml` | Campo **opcional** `vigenteHasta` (instante UTC) de `CotizacionRespuesta` (`POST` y `GET /v1/cotizaciones/...`), y campo obligatorio `vigenteHasta` de `InsumosSuscripcion` (`GET /v1/cotizaciones/{cotizacionId}/insumos-suscripcion`) |
| Quién la usa | `polizas` | Lee `vigenteHasta` de los insumos, decide con su propio reloj (`ahora < vigenteHasta`, `polizas/domain/suscripcion/vigencia.py`) y la guarda en la instantánea de la decisión (DS2-10); la aceptación de HU-W15 validará contra esa instantánea, sin volver a llamar a `cotizacion` |

**Cómo reemplazarla en HU-W04 sin romper `polizas`:**

1. Si W04 crea su entidad oferta con su propia vigencia, calcula `vigenteHasta` con ella en `ConsultarInsumos` (`application/suscripcion/insumos.py`) y en `respuesta_de` (`adapters/inbound/http/cotizaciones.py`), en lugar de `vigente_hasta(...)`. Puede reutilizar `oferta_vigente` para su propio criterio.
2. Conserva **el nombre y el significado** de `vigenteHasta` en ambos contratos: instante UTC y vigencia semiabierta (`ahora < vigenteHasta`). Si cambia el significado, es un cambio incompatible para `polizas` y para el portal.
3. Si W04 crea un identificador propio de la oferta, llena `ofertaId` (y `codigo`, el código legible, CF-S2-10) en `InsumosSuscripcion`: ya existen y hoy van en `null`. `polizas` los guarda y los propaga sin cambios de código (`DecisionSuscripcion.ofertaId` y `cotizacionCodigo`).
4. Mantén en verde el pacto `contracts/pact/polizas-cotizacion.json` (`make test-contract SERVICE=polizas` lo genera y `make test-contract SERVICE=cotizacion` lo verifica) y `tests/contract/test_openapi.py` de `cotizacion` y `bff-web`.
5. Cuando W04 deje de usar el parámetro, retira `oferta_vigencia_horas` de `config.py` y avisa al dueño de `infra/` si se configuró por ambiente.
