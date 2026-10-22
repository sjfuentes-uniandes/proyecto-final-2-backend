# 0012. La suscripción decide antes de la aceptación y `polizas` consulta la oferta

- Estado: propuesto
- Fecha: 2026-10-22 · Decide: por ratificar en la revisión del PR del backend de HU-W13 (`capbmaster` propone; aprueban al menos dos integrantes, uno de ellos el 1) · Historias/ASR: HU-W13, HU-W14, HU-W15, HU-W16, HU-W17; recorrido R3

## Contexto

- El diagrama VA-003A (`docs/arquitectura/diagrama_componentes_responsabilidades.puml`) dibujaba `Cotizacion -> Polizas : envía oferta aceptada`, y la nota (4) de VA-003 dice que una oferta aceptada inicia la suscripción: la aceptación iría **antes** de la decisión.
- Los criterios de HU-W13 (decisión automática sobre una oferta vigente), HU-W14 (comunicar la decisión para saber si se puede continuar la contratación) y HU-W15 (aceptar una oferta aprobada), los mockups E02 («Continuar a la aceptación») y F01 («Paso 1 de 3»), y HU-S02 (`OfertaAceptada → dinero-aceptaciones`) ponen la decisión **antes** de la aceptación (CF-S2-01 del plan del Sprint 2).
- HU-S02 creó el servicio `polizas` (no uno llamado «suscripción», CF-S2-02). La cotización inmutable de HU-W03 vive en `cotizacion` y las reglas versionadas, en `catalogo`.

## Decisión

1. **Orden del recorrido R3:** solicitud de suscripción → decisión (`APROBADA`, `RECHAZADA` o `REVISION_REQUERIDA`) → comunicación → aceptación de una oferta aprobada y vigente → recaudo, firma y emisión (DS2-04).
2. **Dueño:** la decisión y la aceptación viven en `polizas`, en los subpaquetes `suscripcion` y `aceptacion`; HU-W17 agregará `emision` al mismo servicio (DS2-01).
3. **Integración:** `polizas` **consulta** por REST, en paralelo y sin conexión de base abierta, los insumos de la oferta a `cotizacion` (`GET /v1/cotizaciones/{cotizacionId}/insumos-suscripcion`) y las reglas de suscripción vigentes a `catalogo` (`GET /v1/productos/{productoId}/reglas-suscripcion/vigente`), con timeouts de 600 ms y 300 ms (DS2-03, DS2-19). La decisión guarda una instantánea de la oferta y la aceptación valida contra ella, sin volver a llamar a `cotizacion` (DS2-10).
4. El diagrama VA-003A se actualiza en el mismo PR: `Polizas -> Cotizacion : consulta insumos de la oferta` y `Polizas -> Catalogo : consulta reglas de suscripción`, en lugar de `Cotizacion -> Polizas : envía oferta aceptada`.

## Alternativas descartadas

- **`cotizacion` empuja la oferta aceptada a `polizas`** (diagrama original): obliga a `cotizacion` a conocer el ciclo de contratación y pone la aceptación antes de la decisión, en contra de los criterios de HU-W13 a HU-W15.
- **Evento `OfertaCalculada` que `polizas` consume para decidir de forma asíncrona:** agrega consistencia eventual y un Inbox a un paso que el asesor espera en pantalla (E02 muestra «resuelto en 480 ms»), y depende del relay del Outbox, que aún no existe (EXT-04).
- **Servicio nuevo `suscripcion`:** duplica la infraestructura de HU-S02 sin un beneficio para el MVP.

## Consecuencias

- **Se gana:** el orden coincide con los criterios y los mockups; `cotizacion` no conoce la contratación; la decisión es reconstruible con lo que guarda `polizas` y la versión de reglas de `catalogo`.
- **Se sacrifica:** `polizas` depende en línea de `cotizacion` y `catalogo` para decidir (falla cerrada con `503 DEPENDENCIA_NO_DISPONIBLE`, sin decisión), y suma dos saltos por Service Connect (60–100 ms cada uno en `int`, DEF-03).
- **Hay que cambiar:** `infra/platform/catalog.tf` debe agregar `"cotizacion"` y `"catalogo"` a `polizas.calls` (EXT-01, lo hace el dueño de la infraestructura); la nota (4) de VA-003 se lee como «una oferta aprobada y aceptada inicia la contratación».
- Si el equipo no ratifica este ADR, se retira el cambio del `.puml` y la decisión se registra en la bitácora del Sprint 2.
