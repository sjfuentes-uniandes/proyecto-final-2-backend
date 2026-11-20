# Contratos

Contratos que publica el backend para sus consumidores (web, móvil, socios y otros servicios).

## Convención

- **Generados desde el código.** Cada OpenAPI de `openapi/<servicio>.yaml` se genera con `app.openapi()` y una prueba de contrato (`services/<servicio>/tests/contract/test_openapi.py`) verifica que el archivo publicado sea igual a lo que sirve la app. Los esquemas de eventos y del modelo de riesgo se generan desde Pydantic (`TypeAdapter(...).json_schema()`) y también se verifican con una prueba.
- Un cambio de contrato se hace primero aquí; el frontend lo copia con `scripts/sync-contracts.sh <ref>` desde un *commit* de `main`.

## Archivos

| Archivo | Contenido | Dueño | Cómo se regenera |
| --- | --- | --- | --- |
| `openapi/bff-web.yaml` | API de `bff-web`; incluye la lectura de consentimientos del asesor (`GET /v1/clientes/{clienteId}/consentimientos` y `.../{consentimientoId}/historial`, HU-W31, PR 3.5) y, desde HU-W13, la suscripción: `POST /v1/suscripciones`, `GET /v1/suscripciones?cotizacionId=` y `GET /v1/suscripciones/{decisionId}` (grupo `asesores`; la lectura también `suscriptores`), `GET /v1/bandeja-suscripcion` y `.../resumen` (grupo `suscriptores`). Sus DTO son copia de `openapi/polizas.yaml`; las vistas (`DecisionSuscripcionVista`, `CasoBandejaVista`) agregan `clienteNombre` desde `clientes`. Desde HU-W14, la decisión trae el campo opcional `comunicacion` (copia de `ComunicacionDto`), con la que el portal decide qué mostrar. "Cotizaciones abiertas": `GET /v1/cotizaciones` (grupo `asesores`) reenvía el listado de `cotizacion` y su vista `ResumenCotizacionVista` agrega `clienteNombre` desde `clientes`. Desde HU-W17, también expone `GET /v1/cotizaciones/{cotizacionId}/poliza` y `GET /v1/polizas/{polizaId}/documento`; para la póliza activa agrega `titularNombre` desde `clientes`. Desde HU-W18, `GET /v1/polizas/{polizaId}/estado` y `GET /v1/polizas` (grupo `asesores`; filtros `numero`, `ramo`, `estado`, `vigenciaDesde`, `pagina`, `tamano`; estado `NO_INICIADA`, `VIGENTE`, `POR_VENCER` o `VENCIDA` tal como lo deriva `consulta`; 404 uniforme `POLIZA_NO_ENCONTRADA`; `503 SERVICIO_NO_DISPONIBLE` si `consulta` falla, tarda más de 700 ms o rechaza al BFF; sin datos del cliente); desde HU-W19, las rutas del analista de siniestros (`GET /v1/siniestros`, `.../resumen`, `POST .../siguiente`, `GET .../{siniestroId}` y `POST .../{siniestroId}/evidencias/{evidenciaId}/enlace`, grupo `analistas-siniestros`) con las vistas `ResumenSiniestroVista`, `BandejaSiniestrosVista` y `DetalleSiniestroVista` (`clienteNombre` y `polizaVista`, null si `clientes` o `consulta` fallan); desde HU-W20, `POST /v1/siniestros/{siniestroId}/decision` (`Idempotency-Key`; cuerpo `SolicitudDecision` discriminado por `tipo`: `APROBAR`, `RECHAZAR` o `SOLICITAR_SOPORTE`; respuesta `ResultadoDecisionSiniestro`) y `GET /v1/siniestros/catalogos-decision` (causales, documentos, canales y plazos como códigos), mismo grupo; el detalle suma `valorReclamado`, `referenciaPagoPrevista` y `plazoSoporteVenceEn`, y `liquidacion` trae `referenciaPago` y `aprobadaEn`; desde HU-W23, `GET /v1/siniestros/{siniestroId}/pago` (mismo grupo; vista `PagoSiniestroVista` de H08 con `liquidacion`, `cuentaMascarada`, `autorizadoPor`, `historial` e `indemnizacion` (`IndemnizacionVista`, null mientras `dinero` no publica un resultado); plan B: se arma solo con `siniestros`; `404 INDEMNIZACION_NO_ENCONTRADA` si el caso no está `APROBADO`), y `PagoDto` del detalle describe el pago (`PENDIENTE` derivado mientras no hay resultado) | Por confirmar (responsable de `bff-web`) | Ver [Regenerar un contrato](#regenerar-un-contrato) |
| `openapi/api-socios.yaml` | API de `api-socios`: `/admin/socios` (solo `bff-web`; alta, plan y consumo) y rutas del socio (HU-W01, HU-W02). Desde HU-W04, `POST /cotizaciones` y `GET /cotizaciones/{cotizacion_id}` con la oferta pública v1 (`OfertaV1`, lista blanca de campos; 410 `OFERTA_VENCIDA`) | Integrante 1 (`api-socios`) | Ver [Regenerar un contrato](#regenerar-un-contrato) |
| `openapi/publicados/api-socios.v1.yaml` | **Versión v1 publicada a los socios, congelada** (HU-W04 AC2): las operaciones del socio ya disponibles (`/socio/me` y `/cotizaciones`), sin `/admin`, `/health` ni las que aún responden 501. `services/api-socios/tests/contract/test_contrato_publicado.py` falla si el contrato actual elimina o renombra un campo, cambia su tipo, vuelve obligatorio un campo opcional de la solicitud, deja de garantizar un campo de la respuesta, elimina una operación, un código o un valor de un enum. Un cambio incompatible es una versión nueva (v2), no una edición de este archivo | Integrante 1 (`api-socios`) | Solo al publicar una versión: `uv run python services/api-socios/tests/contract/test_contrato_publicado.py` |
| `pact/socio-referencia-api-socios.json` | Pacto de un **socio de referencia** (consumidor) → `api-socios` (HU-W04 AC2): cotiza, consulta y presenta la oferta con solo los campos publicados (matchers de tipo y formato), recotiza ante `410 OFERTA_VENCIDA` y recibe `404 OFERTA_NO_ENCONTRADA` por la oferta de otro socio. Las solicitudes son las que llegan a `api-socios` (con los `X-Partner-*` que agrega API Gateway) | Integrante 1 (`api-socios`) | `make test-contract SERVICE=api-socios` lo genera (`test_pact_socio_referencia.py`) y lo verifica (`test_pact_socio_referencia_proveedor.py`) |
| `pact/api-socios-cotizacion.json` | Pacto consumidor `api-socios` → proveedor `cotizacion` (HU-W04): `POST /v1/cotizaciones` y `GET /v1/cotizaciones/{cotizacionId}` con el actor `SOCIO`, la oferta (`productoId` y `oferta`; lector tolerante), `404 COTIZACION_NO_ENCONTRADA` de la cotización de otro socio y `422 PRODUCTO_FUERA_DE_VIGENCIA`. `cotizacion` lo verifica con su app real y los puertos en memoria (`services/cotizacion/tests/contract/test_pact_api_socios.py`) | Integrante 1 (`api-socios`) | `make test-contract SERVICE=api-socios` lo genera; `make test-contract SERVICE=cotizacion` lo verifica |
| `pact/web-bff-web.json` | Pacto consumidor del portal web (`portal-web`) → proveedor `bff-web` (HU-W14): `GET /v1/suscripciones/{decisionId}` de una aprobada, dos rechazos (SUS-001 y SUS-015), una revisión y el 404 `DECISION_NO_ENCONTRADA`, con la `comunicacion` exacta. HU-W16: `GET /v1/cotizaciones/{cotizacionId}/recaudo` con el recaudo `PENDIENTE`, `CONFIRMADO` y `RECHAZADO`, y el 404 `ACEPTACION_NO_ENCONTRADA` de una cotización sin aceptación. HU-W17: `GET /v1/cotizaciones/{cotizacionId}/poliza` con la póliza `EMITIENDO` y `ACTIVA` (la activa, con el nombre del titular en `titularNombre`; `null` mientras se emite), y el 404 `POLIZA_NO_ENCONTRADA`; `GET /v1/polizas/{polizaId}/documento` con el 200 `{url, venceEn}` y el 409 `POLIZA_NO_ACTIVA` mientras se emite. HU-W19, HU-W20 y HU-W23: los siniestros del analista, su decisión y `GET /v1/siniestros/{siniestroId}/pago` (H08) con el pago confirmado, rechazado y pendiente, el 404 `INDEMNIZACION_NO_ENCONTRADA` y el 403. HU-W08: `GET /operacion/capacidad` (grupo `operacion`) con un escalado terminado en la ventana (`tareas_activas` y `ultimo_escalado` con `fin` y `duracion_s`) y sin escalados ni dato de tareas (`tareas_activas` y `ultimo_escalado` en `null`), con el adaptador de capacidad simulado. Lo genera el portal (`npm run test:pact` en `proyecto-final-2-frontend/web`) y se copia aquí sin editarlo; `bff-web` lo verifica con su app real y `polizas` y `clientes` simulados (`services/bff-web/tests/contract/test_pact_web.py`) | @capbmaster | Portal: `npm run test:pact`; verificación: `make test-contract SERVICE=bff-web` |
| `pact/bff-web-api-socios.json` | Pacto consumidor `bff-web` → proveedor `api-socios` (HU-W01, HU-W02) | Integrante 1 | `make test-contract SERVICE=bff-web` lo genera; `make test-contract SERVICE=api-socios` lo verifica |
| `openapi/bff-movil.yaml` | API de `bff-movil` para la app móvil: sesión, verificación de identidad y registro (HU-M06); `/v1/consentimientos` (otorgar y listar), `/v1/consentimientos/{consentimientoId}` y `/v1/consentimientos/texto-vigente` (HU-M08); `/v1/consentimientos/{consentimientoId}/revocacion` y `.../historial` (HU-M09). El titular sale siempre del token | Por confirmar (responsable de `bff-movil`) | Ver [Regenerar un contrato](#regenerar-un-contrato) |
| `pact/bff-movil-clientes.json` | Pacto consumidor `bff-movil` → proveedor `clientes`: rutas de consentimiento (HU-M08) y su revocación e historial (HU-M09). Se coteja con `openapi/clientes.yaml`; la verificación del lado de `clientes` está pendiente | Por confirmar (responsable de `bff-movil`) | `make test-contract SERVICE=bff-movil` lo genera |
| `pact/bff-movil-consulta.json` | Pacto consumidor `bff-movil` → proveedor `consulta` (HU-M10): lista y detalle de pólizas del cliente con `X-Actor-Id` y `X-Actor-Rol=CLIENTE`; incluye lista vacía, `403 POLIZA_NO_AUTORIZADA` y `404 POLIZA_NO_ENCONTRADA`. Se coteja con `openapi/consulta.yaml`; `consulta` lo verifica como proveedor con su app real y la lectura en memoria (`services/consulta/tests/contract/test_pact_bff_movil.py`, `make test-contract SERVICE=consulta`) | Por confirmar (responsable de `bff-movil`) | `make test-contract SERVICE=bff-movil` lo genera; `make test-contract SERVICE=consulta` lo verifica |
| `pact/bff-web-consulta.json` | Pacto consumidor `bff-web` → proveedor `consulta` (HU-W18): `GET /v1/polizas/{polizaId}/estado` (`200` con el estado y `fechaActualizacion`, y `404 POLIZA_NO_ENCONTRADA`) y `GET /v1/polizas` con filtros, paginación y lista vacía, con `X-Actor-Id` y `X-Actor-Rol=ASESOR`. Los 401, 403 y 5xx de `consulta` no van en el pacto: para el BFF son siempre `503 SERVICIO_NO_DISPONIBLE`. Se coteja con `openapi/consulta.yaml`; `consulta` lo verifica como proveedor con su app real y la lectura en memoria (`services/consulta/tests/contract/test_pact_bff_web.py`, `make test-contract SERVICE=consulta`) | Por confirmar (responsable de `bff-web`) | `make test-contract SERVICE=bff-web` lo genera; `make test-contract SERVICE=consulta` lo verifica |
| `openapi/consulta.yaml` | API interna de lectura de `consulta`: `GET /v1/clientes/{clienteId}/polizas` y `.../{polizaId}` (HU-M10; la llama `bff-movil`; el `clienteId` lo resuelve el BFF desde el `sub` y el actor viaja en `X-Actor-Id` y `X-Actor-Rol=CLIENTE`) y, desde HU-W18, `GET /v1/polizas` y `GET /v1/polizas/{polizaId}/estado` (la llama `bff-web` solo para el grupo `asesores`, con `X-Actor-Rol=ASESOR`; estado `NO_INICIADA`, `VIGENTE`, `POR_VENCER` o `VENCIDA`, derivado de la vigencia; `404` uniforme si la póliza no existe o aún no se proyectó) | Por confirmar (responsable de `consulta`) | Como en [Regenerar un contrato](#regenerar-un-contrato), con `consulta.main` y `contracts/openapi/consulta.yaml` |
| `pact/cotizacion-clientes.json` | Pacto consumidor `cotizacion` → proveedor `clientes`: verificación del consentimiento (`POST /v1/clientes/{clienteId}/consentimientos/verificaciones`) con cada decisión y causa, y `404 CLIENTE_NO_ENCONTRADO` (HU-W31). Se coteja con `openapi/clientes.yaml`; `clientes` lo verifica como proveedor contra Postgres real (`services/clientes/tests/contract/test_pact_proveedor.py`, `make test-contract SERVICE=clientes`, requiere Docker) | Por confirmar (responsable de `cotizacion`) | `make test-contract SERVICE=cotizacion` lo genera |
| `pact/polizas-cotizacion.json` | Pacto consumidor `polizas` → proveedor `cotizacion`: insumos de la oferta para la suscripción (`GET /v1/cotizaciones/{cotizacionId}/insumos-suscripcion` con el actor `ASESOR`), con perfil, sin perfil y `404 COTIZACION_NO_ENCONTRADA` (HU-W13, CT-W13-02). `cotizacion` lo verifica como proveedor con su app real y los puertos en memoria (`services/cotizacion/tests/contract/test_pact_polizas.py`) | @capbmaster | `make test-contract SERVICE=polizas` lo genera; `make test-contract SERVICE=cotizacion` lo verifica |
| `pact/dinero-adaptador-pagos.json` | Pacto consumidor `dinero` → proveedor `adaptador-pagos` (HU-W16, 16.10): `POST /v1/cobros` con la `claveRecaudo`, el monto como texto decimal, `COP`, el `pagadorRef` sintético y `X-Correlation-Id`; solo lo que el proveedor promete, `200 APROBADO` (con `transaccionId`) y `200 RECHAZADO` (con `motivo`). Desde la 16.11, también la consulta de la conciliación, `GET /v1/cobros/{claveRecaudo}?pagadorRef` con `X-Correlation-Id`: `200 APROBADO`, `200 RECHAZADO` y `404 COBRO_NO_ENCONTRADO` (dinero solo lee `codigo`). Los timeouts, los 5xx y los cuerpos fuera de contrato van en las unitarias y en las de módulo. `adaptador-pagos` lo verifica con su app real y el proveedor simulado (`services/adaptador-pagos/tests/contract/test_pact_dinero.py`) | Por confirmar (responsable de `dinero`) | `make test-contract SERVICE=dinero` lo genera; `make test-contract SERVICE=adaptador-pagos` lo verifica |
| `pact/polizas-adaptador-firma.json` | Pacto consumidor `polizas` → proveedor `adaptador-firma` (HU-W17, 17.8): `POST /v1/firmas` de la tarea emisora con `documentoId` (= `polizaId`), la `huella` sha256 del PDF, `firmanteRef` (= `clienteId`, sintético) y `X-Correlation-Id`; solo lo que el proveedor promete, `200 FIRMADO` con el mismo `documentoId` y la misma `huella`. Los timeouts, los 5xx y los cuerpos fuera de contrato van en las unitarias y en las de módulo. `adaptador-firma` lo verifica con su app real y el proveedor simulado (`services/adaptador-firma/tests/contract/test_pact_polizas.py`) | @capbmaster | `make test-contract SERVICE=polizas` lo genera; `make test-contract SERVICE=adaptador-firma` lo verifica |
| `openapi/adaptador-datos.yaml` | API `/v1/consultas` de `adaptador-datos` (HU-W10) | @capbmaster | Ver [Regenerar un contrato](#regenerar-un-contrato) |
| `openapi/adaptador-identidad.yaml` | API `/v1/verificaciones` de `adaptador-identidad`: verifica la selfie y la prueba de vida con el proveedor KYC (HU-M06) | Por confirmar (responsable de `adaptador-identidad`) | Ver [Regenerar un contrato](#regenerar-un-contrato) |
| `openapi/adaptador-pagos.yaml` | API interna de `adaptador-pagos` (HU-W16; solo la llama `dinero`): `POST /v1/cobros` cobra el primer recaudo de la prima con `claveRecaudo` como clave idempotente hacia el proveedor, y `GET /v1/cobros/{claveRecaudo}?pagadorRef=` consulta su estado para conciliar (`404 COBRO_NO_ENCONTRADO`). Desde HU-W23, `POST /v1/desembolsos` ordena el pago de la indemnización con `referencia` (`IND-<codigo>-01`) como clave idempotente hacia el proveedor y `GET /v1/desembolsos/{referencia}?beneficiarioRef=` consulta su estado para conciliar (`404 DESEMBOLSO_NO_ENCONTRADO`), con las mismas respuestas de error que los cobros. Monto como texto decimal; sin datos de tarjeta ni de cuenta (ADR 0013, ADR 0015) | Por confirmar (responsable de `adaptador-pagos`) | Como en [Regenerar un contrato](#regenerar-un-contrato), con `adaptador_pagos.main` y `contracts/openapi/adaptador-pagos.yaml` (en Windows, `open(..., 'w', encoding='utf-8')`) |
| `openapi/dinero.yaml` | API interna de `dinero` (HU-W23; primera ruta HTTP del servicio, la llamará `bff-web` en la sincronización final): `GET /v1/indemnizaciones?siniestroId=` → `{items: [IndemnizacionDto]}` con `id`, `siniestroId`, `tipo` (`INDEMNIZACION`), `referencia`, `monto` (texto decimal), `moneda`, `estado` (`PENDIENTE`, `CONFIRMADA` o `RECHAZADA`), `transaccionId`, `motivo`, `intentos`, `duplicadosDescartados`, `creadaEn`, `confirmadaEn` y `ultimoIntentoEn`. Actor `X-Actor-Id` y `X-Actor-Rol` (`ANALISTA_SINIESTROS`). Sin indemnización responde 200 con `items` vacío. Sin datos de la cuenta del beneficiario (ADR 0015) | Por confirmar (responsable de `dinero`) | Ver [Regenerar un contrato](#regenerar-un-contrato) |
| `openapi/adaptador-firma.yaml` | API interna de `adaptador-firma` (HU-W17; solo la llama `polizas`): `POST /v1/firmas` firma la huella sha256 del documento de póliza (`documentoId`, `huella` `sha256:<64 hex>` y `firmanteRef`), con `documentoId` como clave idempotente hacia el proveedor. Solo vale la firma del mismo documento y la misma huella; si no, `502 RESPUESTA_INVALIDA`. Sin datos personales: el firmante es el `clienteId` sintético (ADR 0014) | Por confirmar (responsable de `adaptador-firma`) | Como en [Regenerar un contrato](#regenerar-un-contrato), con `adaptador_firma.main` y `contracts/openapi/adaptador-firma.yaml` (en Windows, `open(..., 'w', encoding='utf-8')`) |
| `openapi/clientes.yaml` | API de `clientes`: `/v1/clientes/actual`, `/v1/clientes/{clienteId}/identidad` y `/v1/clientes/actual/identidad/verificaciones` (HU-M06); `/v1/clientes/actual/consentimientos` (otorgar y listar), `/v1/clientes/actual/consentimientos/{consentimientoId}` y `/v1/consentimientos/textos/vigente` (HU-M08); `.../{consentimientoId}/revocacion` y `.../{consentimientoId}/historial` (HU-M09); `/v1/clientes/{clienteId}/consentimientos/verificaciones` (HU-W31); lectura web para el asesor (`X-Actor-Rol=ASESOR`, solo `GET`): `/v1/clientes/{clienteId}/consentimientos` y `.../{consentimientoId}/historial` (HU-W31, PR 3.3). `cotizacion` coteja sus DTO de identidad y verificación con este archivo (`services/cotizacion/tests/contract/test_dto_clientes.py`); `bff-web` coteja los de alta, identidad y lectura de consentimientos (`services/bff-web/tests/contract/test_openapi.py`) | Por confirmar (responsable de `clientes`) | Ver [Regenerar un contrato](#regenerar-un-contrato) |
| `events/ConsentimientoOtorgado.v1.schema.json` | Evento `ConsentimientoOtorgado` v1 del outbox de `clientes` (HU-M08); `aggregateVersion` es opcional y vale 1 | Por confirmar (responsable de `clientes`) | `uv run python -m clientes.adapters.outbound.eventos` (desde la raíz) |
| `events/ConsentimientoRevocado.v1.schema.json` | Evento `ConsentimientoRevocado` v1 del outbox de `clientes` (HU-M09); `aggregateVersion` es opcional y vale 2 | Por confirmar (responsable de `clientes`) | `uv run python -m clientes.adapters.outbound.eventos` (desde la raíz) |
| `events/VerificacionConsentimientoRegistrada.v1.schema.json` | Evento `VerificacionConsentimientoRegistrada` v1 del outbox de `clientes` (HU-W31): una fila por verificación; `aggregateId` = `clienteId`, sin `aggregateVersion`; solo para `auditoria` (sin consumidor hasta la PR 2.2 y el mapeo de la 3.3) | Por confirmar (responsable de `clientes`) | `uv run python -m clientes.adapters.outbound.eventos` (desde la raíz) |
| `openapi/catalogo.yaml` | API interna de `catalogo`: `/v1/modelos-riesgo` (HU-W11), `/v1/productos/{productoId}`, `.../reglas-prima/vigente?fecha=` y `.../reglas-prima/{version}` (HU-W03), y `.../reglas-suscripcion/vigente?fecha=` y `.../reglas-suscripcion/{version}` (HU-W13), y `.../reglas-fallback/vigente?fecha=` y `.../reglas-fallback/{version}` (HU-W06). La consumen `cotizacion` y `polizas` Desde HU-W21, `ProductoRespuesta` suma el bloque **opcional** `parametrico {tipoEvento, unidad, umbral, valorPago, versionRegla}` (la zona y la ventana son de cada póliza): solo lo trae el producto de QA `clima-lluvia` (`LLUVIA_ACUMULADA`, `mm`, umbral `120`, pago `1400000.00` COP, semilla de `CATALOGO_PRODUCTOS_QA`); sin él la clave no se serializa, así que la respuesta de los demás productos no cambia. | @capbmaster (modelos y reglas de suscripción); HU-W03 (productos) | Ver [Regenerar un contrato](#regenerar-un-contrato) |
| `openapi/cotizacion.yaml` | API interna de `cotizacion`: `/v1/perfiles` (HU-W10 a HU-W31) y, desde HU-W03, `POST /v1/cotizaciones`, `GET /v1/cotizaciones/{cotizacionId}` y `GET /v1/productos/{productoId}/condiciones` (actores `ASESOR` y `SOCIO`). Desde HU-W13, `GET /v1/cotizaciones/{cotizacionId}/insumos-suscripcion` (actor `ASESOR`, lo consume `polizas`) y el campo opcional `vigenteHasta` de `CotizacionRespuesta` (DS2-20). `bff-web` copia sus DTO y `tests/contract/test_openapi.py` de `bff-web` verifica que sean iguales. Desde HU-W04, el campo opcional `oferta` de `CotizacionRespuesta` (`OfertaDto`: código, precio, coberturas con su suma, exclusiones, vigencia y estado al consultar) y `ofertaId` y `codigo` llenos en los insumos de suscripción. "Cotizaciones abiertas": `GET /v1/cotizaciones` lista las cotizaciones con oferta de la más reciente a la más antigua (`ResumenCotizacionDto`), con filtros `estado` (de la oferta al consultar) y `q` (parte del código), paginado por cursor; un `SOCIO` solo ve las suyas | Por confirmar (responsable de `cotizacion`) | Ver [Regenerar un contrato](#regenerar-un-contrato) |
| `modelos/reglas-prima.v1.schema.json` | Esquema de la definición de las reglas de prima (HU-W03): tasa base, factores por nivel (`BAJO`, `MEDIO`, `ALTO`, `SIN_PERFIL`) y por variable, recargos por cobertura opcional, prima mínima y máxima, redondeo (escala y modo) y límites. Decimales como texto. Desde HU-W04, el bloque opcional `oferta` (`vigenciaMinutos`, `exclusiones` y `sumasCoberturas`), que no se serializa cuando falta: el hash de las reglas anteriores no cambia. `cotizacion` tiene una copia (`DefinicionReglasPrimaCotizacion`) y CT-W03-01 verifica que sea igual | HU-W03, HU-W04 | `uv run python -m catalogo.adapters.inbound.http.esquema_prima > contracts/modelos/reglas-prima.v1.schema.json` |
| `openapi/polizas.yaml` | API interna de `polizas` (HU-W13): `POST /v1/suscripciones` (actor `ASESOR`, `Idempotency-Key`), `GET /v1/suscripciones?cotizacionId=`, `GET /v1/suscripciones/{decisionId}` y `.../reconstruccion` (`ASESOR` o `SUSCRIPTOR`; las reglas `RESERVADA` llegan sin `valorCaso` para `ASESOR`, DS2-07), `GET /v1/suscripciones/bandeja` y `.../resumen` (`SUSCRIPTOR`). Desde HU-W14, la decisión del `POST` y del `GET /v1/suscripciones/{decisionId}` trae el campo opcional `comunicacion` (`ComunicacionDto`: condición, si admite contratación, siguiente paso, vigencia, motivo para el cliente, alternativas y plazo de respuesta), derivado al leer y sin códigos ni valores de reglas. HU-W15 agrega aceptación y HU-W16 el estado del recaudo; HU-W17 agrega la consulta de la póliza y el documento para `ASESOR` o `SUSCRIPTOR`, además de su verificación pública. La consume `bff-web` | @capbmaster (suscripción) | Ver [Regenerar un contrato](#regenerar-un-contrato) |
| `openapi/siniestros.yaml` | API interna de `siniestros` (HU-W19; la llama `bff-web`): bandeja con cursor y `total`, resumen, `POST /v1/siniestros/siguiente` (`Idempotency-Key`, `FOR UPDATE SKIP LOCKED`), detalle con verificación de cobertura y coherencia de la evidencia, `POST .../evidencias/{evidenciaId}/enlace` (URL prefirmada ≤ 300 s, nunca guardada) y `POST /v1/siniestros/accesos-denegados`. Actores `X-Actor-Id`, `X-Actor-Rol` (`ANALISTA_SINIESTROS`) y `X-Actor-Nombre`. Es el agregado compartido con M01 a M05 (DS3-01). Desde HU-W20, `POST /v1/siniestros/{siniestroId}/decision` (decisión idempotente del analista asignado: aprobar con liquidación, rechazar con causal y explicación o solicitar soporte) y `GET /v1/siniestros/catalogos-decision`; el detalle suma `valorReclamado`, `referenciaPagoPrevista` y `plazoSoporteVenceEn`, y `liquidacion` (la llena al aprobar) trae `referenciaPago` y `aprobadaEn`. La sección `evento` del detalle viene en null hasta HU-W22; desde HU-W23, `pago` (`PagoDto`) es null mientras el caso no esté `APROBADO` y `PENDIENTE` mientras `dinero` no haya publicado un resultado (derivado al leer; ADR 0015), y el historial suma `PAGO_CONFIRMADO` y `PAGO_RECHAZADO` | `@capbmaster` | Ver [Regenerar un contrato](#regenerar-un-contrato) |
| `modelos/reglas-suscripcion.v1.schema.json` | Esquema de la definición de las reglas de suscripción (HU-W13): producto, alternativas informativas (E03) y reglas `{codigo SUS-NNN, orden, tipo EXCLUYENTE\|REVISION, descripcion (clave de texto), campo, operador, valor, visibilidad EXPLICABLE\|RESERVADA, motivoCliente}`. `campo`, `operador` y `motivoCliente` son conjuntos cerrados; los valores numéricos van como texto decimal | @capbmaster | `uv run python -m catalogo.adapters.inbound.http.esquema_suscripcion > contracts/modelos/reglas-suscripcion.v1.schema.json` |
| `modelos/reglas-fallback.v1.schema.json` | Esquema de la definición de las reglas de fallback (HU-W06): producto y reglas `{id FB-NNN, fuente, obligatoria, accion DEGRADAR\|PENDIENTE, ajuste (factor en texto decimal o null), mensajeCodigo}`. Una fuente sin regla vigente no es degradable; `obligatoria` debe coincidir con el modelo de riesgo y una obligatoria nunca se degrada (validación de `catalogo`). `cotizacion` guarda una copia del consumidor (CT-W06-01) | HU-W06 | `uv run python -m catalogo.adapters.inbound.http.esquema_fallback > contracts/modelos/reglas-fallback.v1.schema.json` |
| `events/DecisionSuscripcionRegistrada.v1.schema.json` | Evento `DecisionSuscripcionRegistrada` v1 del outbox de `polizas` (HU-W13): una fila por decisión, en la misma transacción; `aggregateId` = `decisionId`. Sin montos ni datos personales: resultado, reglas, versión y huellas. Para `auditoria` | @capbmaster | `uv run python -m polizas.adapters.outbound.eventos` (desde la raíz) |
| `events/CotizacionDegradada.v1.schema.json` | Evento `CotizacionDegradada` v1 del outbox de `cotizacion` (HU-W06): una fila por decisión de fallback, en la misma transacción que la cotización degradada (`aggregateId` = `cotizacionId`) o sola si queda pendiente (`aggregateId` = `decisionId`). `decision` DEGRADADA\|PENDIENTE, `faltantes[{fuente, dato, motivo, obligatoria, reglaId, reglaVersion, reglaAccion}]`, versión y huella de las reglas. Sin cliente, montos ni datos declarados. Para `auditoria` | HU-W06 | `uv run python -m cotizacion.adapters.outbound.eventos` (desde la raíz) |
| `events/RecaudoConfirmado.v1.schema.json` | Evento `RecaudoConfirmado` v1 del outbox de `dinero` (HU-W16, 16.10): el primer cobro de la prima quedó `CONFIRMADO`; se escribe en la misma transacción que el resultado del movimiento. `aggregateId` = `recaudoId`; payload `recaudoId`, `claveRecaudo`, `cotizacionId`, `monto {valor, moneda}` (texto decimal, a lo sumo 2 decimales), `transaccionId` y `confirmadoEn`. Sin datos del pagador. Para `polizas-recaudos`, `avisos` y `auditoria` | Por confirmar (responsable de `dinero`) | `uv run python -m dinero.adapters.outbound.eventos` (desde la raíz) |
| `events/RecaudoRechazado.v1.schema.json` | Evento `RecaudoRechazado` v1 del outbox de `dinero` (HU-W16, 16.10): el proveedor rechazó el cobro. `aggregateId` = `recaudoId`; payload `recaudoId`, `claveRecaudo`, `cotizacionId`, `motivo` (código; `MOTIVO_NO_INFORMADO` si el proveedor no trae uno) y `rechazadoEn`. Sin datos del pagador. Para `polizas-recaudos` y `auditoria` | Por confirmar (responsable de `dinero`) | `uv run python -m dinero.adapters.outbound.eventos` (desde la raíz) |
| `events/IndemnizacionConfirmada.v1.schema.json` | Evento `IndemnizacionConfirmada` v1 del outbox de `dinero` (HU-W23, DS3-17; ADR 0015): la pasarela confirmó la indemnización; se escribe en la misma transacción que el resultado. `aggregateId` = `indemnizacionId`; payload `indemnizacionId`, `siniestroId`, `referencia` (`IND-<codigo>-01`), `monto` y `moneda` (texto decimal, a lo sumo 2 decimales), `transaccionId`, `intentos`, `duplicadosDescartados` y `confirmadaEn`. `intentos` y `duplicadosDescartados` los usa el plan B de H08 (DS3-26). Sin datos de la cuenta. Para `siniestros-indemnizaciones` (prefijo `Indemnizacion`), `consulta` y `auditoria` | Por confirmar (responsable de `dinero`) | `uv run python -m dinero.adapters.outbound.eventos` (desde la raíz) |
| `events/IndemnizacionRechazada.v1.schema.json` | Evento `IndemnizacionRechazada` v1 del outbox de `dinero` (HU-W23, AC4): la pasarela rechazó la indemnización. `aggregateId` = `indemnizacionId`; payload `indemnizacionId`, `siniestroId`, `referencia`, `motivo` (código normalizado de la pasarela; `MOTIVO_NO_INFORMADO` si no trae uno), `intentos`, `duplicadosDescartados` y `rechazadaEn`. Sin datos de la cuenta. Mismos consumidores. El siniestro queda con el «pago por gestionar» | Por confirmar (responsable de `dinero`) | `uv run python -m dinero.adapters.outbound.eventos` (desde la raíz) |
| `events/PolizaEmitida.v1.schema.json` | Evento `PolizaEmitida` v1 del outbox de `polizas` (HU-W17, 17.2; ADR 0014): la póliza quedó `ACTIVA` con su documento firmado; una por aceptación. `aggregateId` = `polizaId`; `type` fijo. Payload `polizaId`, `numero` (`100-<año>-<6 dígitos>`), `cotizacionId`, `claveRecaudo`, `clienteId`, `producto {id, version}`, `ramo {codigo, nombre}` (el código de tres dígitos con el que empieza `numero`) y `coberturas [{codigo, nombre, obligatoria}]` (al menos una; HU-W17, 17.8b: tomadas de la instantánea de la oferta aceptada, no del catálogo vigente), `vigencia {desde, hasta}` (fechas inclusivas, America/Bogota), `prima` y `sumaAsegurada` (texto decimal, a lo sumo 2 decimales), `moneda`, `periodicidad`, `documento {clave, versionId, huella, tamanoBytes}` (la misma referencia si se reentrega), `firma {firmaId, selloTiempo}`, `emitidaEn` y, desde HU-W21 (DS3-20, opción A, aditivo), `coberturaParametrica {tipoEvento, zona, umbral, unidad, valorPago, moneda, versionRegla}`: **opcional y siempre `null` por ahora** (`polizas` todavía no la llena; `pagos-parametricos` solo crea una cobertura si viene). Sin nombres, documentos de identidad ni datos de contacto. Para `avisos`, `consulta-proyeccion` y `auditoria`; se deduplica por `eventId`. Productor implementado en HU-W17, 17.8 | @capbmaster | `uv run python -m polizas.adapters.outbound.eventos` (desde la raíz) |
| `events/SiniestroEnEvaluacion.v1.schema.json` | Evento `SiniestroEnEvaluacion` v1 del outbox de `siniestros` (HU-W19): el caso quedó en evaluación de un analista: lo tomó (`motivo = ASIGNACION`) o venció el plazo del soporte y volvió a su bandeja (`motivo = PLAZO_SOPORTE_VENCIDO`, HU-W20: cambio aditivo del enum); `aggregateId` = `siniestroId`. Sin nombres | `@capbmaster` | `uv run python -m siniestros.adapters.outbound.eventos` |
| `events/SiniestroConsultado.v1.schema.json` | Evento `SiniestroConsultado` v1 (HU-W19): se abrió el detalle; solo para `auditoria`. `consulta` lo ignora (CF-S3-04) | `@capbmaster` | `uv run python -m siniestros.adapters.outbound.eventos` |
| `events/EvidenciaConsultada.v1.schema.json` | Evento `EvidenciaConsultada` v1 (HU-W19): se emitió una URL prefirmada; `siniestroId`, `evidenciaId`, `actorId` y `expiraEn`, **sin la URL ni la clave del objeto**; solo para `auditoria` | `@capbmaster` | `uv run python -m siniestros.adapters.outbound.eventos` |
| `events/AccesoSiniestroDenegado.v1.schema.json` | Evento `AccesoSiniestroDenegado` v1 (HU-W19, AC3): acceso rechazado por `bff-web` (`BFF_WEB`, sin el grupo) o por `siniestros` (`SINIESTROS`, sin el rol); solo para `auditoria` | `@capbmaster` | `uv run python -m siniestros.adapters.outbound.eventos` |
| `events/SiniestroAprobado.v1.schema.json` | Evento `SiniestroAprobado` v1 (HU-W20, DS3-24): único para R5 y R7; `siniestroId`, `codigo`, `origen`, `polizaId`, `clienteId`, `valorIndemnizar`, `moneda`, `referenciaPago` (`IND-<codigo>-01`), `beneficiarioRef` (opaco, **nunca la cuenta**), `aprobadoEn` y `decididoPor` (null si lo aprueba un paramétrico). Lo consume `dinero` (HU-W23, cola `dinero-indemnizaciones`) y lo proyecta `consulta`. `aggregateId` = `siniestroId` | `@capbmaster` | `uv run python -m siniestros.adapters.outbound.eventos` |
| `events/SiniestroRechazado.v1.schema.json` | Evento `SiniestroRechazado` v1 (HU-W20): `siniestroId`, `codigo`, `polizaId`, `causal`, `decididoPor` y `decididoEn`. **Sin la explicación al cliente** (texto libre). Lo proyecta `consulta` | `@capbmaster` | `uv run python -m siniestros.adapters.outbound.eventos` |
| `events/SiniestroSoporteSolicitado.v1.schema.json` | Evento `SiniestroSoporteSolicitado` v1 (HU-W20): `siniestroId`, `codigo`, `polizaId`, `documentos`, `canal`, `plazoVenceEn` y `decididoPor`. **Sin el mensaje al cliente**. Lo proyecta `consulta` | `@capbmaster` | `uv run python -m siniestros.adapters.outbound.eventos` |
| `datos-sinteticos/siniestros-r7.json` | Ocho siniestros sintéticos de R7 (HU-W19, DS3-07): tres `REPORTADO` sin asignar, cinco `EN_EVALUACION` del analista QA (uno fuera del compromiso, uno con objeto ausente, uno con objeto alterado y uno con la póliza vencida en la fecha del evento), con sus objetos (PNG, PDF y MP4 mínimos) en base64 y su sha256. La semilla de QA de `siniestros` los usa; instantes relativos al sembrar | `@capbmaster` | Por un generador fuera del repositorio (`docs/sprint_3/evidencias/hu-w19/prototipos/generar_datos_r7.py`) |
| `events/DescarteParametricoResuelto.v1.schema.json` | Evento `DescarteParametricoResuelto` v1 del outbox de `pagos-parametricos` (HU-W24, decisión B2): un analista reprocesó, corrigió y reprocesó, o descartó un mensaje de la DLQ de `parametricos-eventos`. `aggregateId` = `eventId` resuelto (o el `MessageId`). Payload `messageId`, `eventoId`, `accion`, `causa`, `actorId`, `motivo`, `original` (el cuerpo tal como estaba), `corregido` (solo en `CORREGIDO`) y `resueltoEn`. Solo para `auditoria` | HU-W24 | `uv run python -m pagos_parametricos.adapters.outbound.eventos` (desde la raíz) |
| `openapi/pagos-parametricos.yaml` | API interna de `pagos-parametricos`: cola de descarte `/v1/parametricos/descartes` (listar, detalle, `reproceso`, `correccion`, `descarte`; rol `ANALISTA_SINIESTROS`). La consume `bff-web` (HU-W24, decisión B1) | HU-W24 | Ver `services/pagos-parametricos/tests/contract/test_openapi.py` |
| `openapi/adaptador-telemetria.yaml` | API de entrada de `adaptador-telemetria` (HU-W21, DS3-16): `POST /v1/eventos` (API Gateway la publica como `/telemetria/v1/eventos`, sin JWT). Cuerpo `EventoExterno` v1 (`versionContrato` `"1"`, `idExterno`, `eventoOrigen`, `tipoEvento` `LLUVIA_ACUMULADA` o `RETRASO_VUELO`, `valorObservado` texto decimal, `unidad` `mm` o `min`, `zona`, `observadoEn`, `ubicacion` opcional). Encabezados `X-Telemetria-Fuente`, `X-Telemetria-Marca-Tiempo` y `X-Telemetria-Firma` (`sha256=<hex>` de HMAC-SHA256 sobre `<marca-tiempo>.<cuerpo crudo>`). Respuestas 202 (nuevo), 200 (reenvío: mismo `eventoId`), 401 `FIRMA_INVALIDA` o `MARCA_TIEMPO_FUERA_DE_RANGO`, 422 `CONTRATO_INVALIDO` y 503. El esquema del cuerpo se publica con `publicar_esquemas` porque la ruta lee el cuerpo crudo antes de parsearlo. Detalle: [`docs/hu-w21-evento-parametrico.md`](../docs/hu-w21-evento-parametrico.md) | @capbmaster | Ver [Regenerar un contrato](#regenerar-un-contrato) |
| `events/EventoParametricoObservado.v1.schema.json` | Evento `EventoParametricoObservado` v1 del outbox de `adaptador-telemetria` (HU-W21, W21-B02): un evento externo auténtico y con contrato válido, ya archivado. `eventId` = `aggregateId` = `eventoId` (uuid5 de `telemetria/<fuente>/<idExterno>`, DS3-16); `occurredAt` = `recibidoEn`. Payload `eventoId`, `fuente`, `idExterno`, `eventoOrigen`, `tipoEvento`, `valorObservado` (texto decimal), `unidad`, `zona`, `observadoEn`, `recibidoEn` y `archivoRef` (clave del original en `telemetria/<AAAA>/<MM>/<DD>/<eventoId>.json`). Sin cuerpo crudo, firma ni `ubicacion`. Para `pagos-parametricos` (cola `parametricos-eventos`) y `auditoria`; el reproceso de W24 (§7) lo reconstruye desde el original | @capbmaster | `uv run python -m adaptador_telemetria.adapters.outbound.eventos` (desde la raíz) |
| `events/EventoParametricoEvaluado.v1.schema.json` | Evento `EventoParametricoEvaluado` v1 del outbox de `pagos-parametricos` (HU-W21, W21-B02): resultado de evaluar un evento observado, una sola vez. `aggregateId` = `eventoId`. Payload `eventoId`, `fuente`, `idExterno`, `eventoOrigen`, `decision` (`ACTIVA`, `NO_ACTIVA` o `SIN_POLIZAS`), `razon` (`SIN_POLIZAS_EN_ZONA` o `null`), `activaciones`, `evaluadas`, `versionRegla` y `evaluadoEn`. Sin pólizas ni valores: la razón por póliza vive en `evaluaciones`. Para `auditoria` (y el seguimiento H09 de W22) | @capbmaster | `uv run python -m pagos_parametricos.adapters.outbound.eventos` (desde la raíz) |
| `datos-sinteticos/eventos-parametricos.json` | Datos sintéticos de R5 (HU-W21 y HU-W22, plan §1.8): diez coberturas (`PolizaEmitida.v1` con `coberturaParametrica`: cinco de la matriz de evaluación y cinco del orden de eventos), cinco `PolizaCancelada` sintéticas, cinco escenarios de orden (DS3-22), 19 envíos de la fuente de telemetría (válido, justo en el umbral, una unidad bajo, límites de ventana, fuera de zona, retraso de vuelo, duplicado, firma inválida o ausente, fuente desconocida, marca vieja y futura, malformado, versión no soportada, unidad incoherente y campo de más) y tres mensajes directos a la cola (entrega duplicada y dos envenenados). Cada caso trae lo esperado (HTTP, archivo, filas de Outbox y la decisión por póliza). Sin secretos: el HMAC se calcula al enviar (`scripts/dev/firmar_evento.py`) | @capbmaster | Por un generador fuera del repositorio (`docs/sprint_3/evidencias/hu-w21/prototipos/generar_datos_w21.py`) |
| `datos-sinteticos/cotizaciones-suscripcion.json` | Cotizaciones sintéticas S01–S11 para la suscripción (HU-W13): aprobada, rechazadas (edad 68, plazo 300, listas restrictivas), revisiones (suma y confianza, suma, perfil insuficiente, sin perfil), oferta vencida e identidad no verificada o pendiente. Desde HU-W04, S01 y S08 (los casos del pacto `polizas-cotizacion`) traen `ofertaId` y `codigo`; los demás siguen en `null`, como una cotización anterior a la oferta. Cada caso trae los `insumos` que usan los dobles de `polizas`, cómo crearlo en local sobre un cliente de `clientes-perfilamiento.json` y el resultado esperado | @capbmaster | A mano |
| `alcances-perfilamiento.json` | Propósito, alcances (fuentes), tipo, textos es-CO / en-US y campos canónicos | @capbmaster | A mano |
| `datos-sinteticos/clientes-perfilamiento.json` | Clientes sintéticos C01–C33 (C20–C27, casos de fallback de HU-W06 con `esperado.cotizacion`; C28–C33, clones de C01 con cobro y firma aprobados en el simulador): identidad, consentimiento, datos declarados, comportamiento del simulador y resultado esperado | @capbmaster | A mano. Después de cambiarlo, regenerar los mapeos de los aliados: `python services/simulador-aliados/scripts/generar_mapeos.py todo`. Las pruebas de módulo de `cotizacion` siembran `clientes` real desde este archivo (`services/cotizacion/tests/module/conftest.py`); `test_clientes_real_decide_lo_que_dicen_los_datos_sinteticos` (`make test-module SERVICE=cotizacion`) falla si la siembra no reproduce un caso |
| `codigos-error.md` | Códigos de error estables | Todos | A mano |
| `encabezados.md` | Encabezados entre servicios: `X-Deadline-Ms` (HU-W05), quién lo inicia, lo envía y lo respeta, y por qué es un restante relativo | Todos | A mano |
| `operaciones.md` | Nombres de operaciones medidas | Todos | A mano |

## Regenerar un contrato

Si cambia la API de un servicio, regenerar su archivo desde la raíz del repositorio. La prueba `tests/contract/test_openapi.py` del servicio falla mientras el archivo publicado no coincida con lo que sirve la app.

```bash
uv run python -c "import yaml; from adaptador_datos.main import app; open('contracts/openapi/adaptador-datos.yaml', 'w').write(yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True))"
```

```bash
uv run python -c "import yaml; from adaptador_identidad.main import app; open('contracts/openapi/adaptador-identidad.yaml', 'w').write(yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True))"
```

```bash
uv run python -c "import yaml; from clientes.main import app; open('contracts/openapi/clientes.yaml', 'w').write(yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True))"
```

```bash
uv run python -c "import yaml; from bff_web.main import app; open('contracts/openapi/bff-web.yaml', 'w').write(yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True))"
```

```bash
uv run python -c "import yaml; from bff_movil.main import app; open('contracts/openapi/bff-movil.yaml', 'w', encoding='utf-8').write(yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True))"
```

```bash
uv run python -c "import yaml; from api_socios.main import app; open('contracts/openapi/api-socios.yaml', 'w').write(yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True))"
```

```bash
uv run python -c "import yaml; from catalogo.main import app; open('contracts/openapi/catalogo.yaml', 'w').write(yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True))"
```

```bash
uv run python -c "import yaml; from cotizacion.main import app; open('contracts/openapi/cotizacion.yaml', 'w').write(yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True))"
```

```bash
uv run python -c "import yaml; from siniestros.main import app; open('contracts/openapi/siniestros.yaml', 'w').write(yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True))"
```

```bash
uv run python -c "import yaml; from dinero.main import app; open('contracts/openapi/dinero.yaml', 'w').write(yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True))"
```

```bash
uv run python -c "import yaml; from adaptador_telemetria.main import app; open('contracts/openapi/adaptador-telemetria.yaml', 'w').write(yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True))"
```

```bash
uv run python -c "import yaml; from polizas.main import app; open('contracts/openapi/polizas.yaml', 'w').write(yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True))"
```

Después de cambiar `cotizacion.yaml`, regenerar también `bff-web.yaml` si cambiaron los DTO que copia (cotizaciones y perfiles). Lo mismo con `polizas.yaml` y los DTO de suscripción (HU-W13).

Después de cambiar `adaptador-datos.yaml`, avisar a `cotizacion` (usa `AdaptadorDatosHttp`). Después de cambiar `bff-web.yaml`, sincronizar el frontend con `scripts/sync-contracts.sh <ref>`.

## Caché de catálogo (HU-W03)

Contrato entre `catalogo` (escribe e invalida) y `cotizacion` (solo lee), según los diagramas de componentes, despliegue e información. Valkey de HU-S02 (`CACHE_URL`, `rediss://`). El valor de cada clave es el JSON exacto que respondería la API de `catalogo` (`ProductoRespuesta` o `ReglasPrimaRespuesta` de `openapi/catalogo.yaml`); `services/catalogo/tests/contract/test_esquema_prima.py` lo verifica.

| Clave | Valor | Vida | Cuándo la escribe `catalogo` |
| --- | --- | --- | --- |
| `catalogo:v1:producto:{productoId}` | `ProductoRespuesta` | `SOLVENTA_CACHE_TTL_S` (60 s) | Al leer el producto de su base sin encontrarlo en la caché |
| `catalogo:v1:reglas-prima:{productoId}:vigente` | `ReglasPrimaRespuesta` de las reglas vigentes **ahora** | 60 s | Al resolver las reglas vigentes para una fecha a menos de 60 s de ahora (una consulta histórica no la toca) |
| `catalogo:v1:reglas-prima:{productoId}:version:{version}` | `ReglasPrimaRespuesta` | 24 h | Al leer una versión cerrada (no cambia nunca) |

- **Invalidación:** al arrancar, `catalogo` borra el producto y sus reglas vigentes de cada producto de la semilla (un despliegue nuevo no deja datos viejos más de lo que tarda en arrancar). Un cambio de reglas es una versión nueva: la clave `vigente` se renueva en ≤ 60 s.
- **Lectura en `cotizacion`:** usa las reglas guardadas solo si su `vigenteDesde` ≤ la fecha de la cotización; un valor fuera del contrato o una falla de Valkey (timeout de 50 ms, circuito) es un fallo de caché y va a `catalogo` por HTTP. Sin `CACHE_URL`, una caché TTL en memoria por réplica.
- **Prefijo `v1`:** si cambia la forma del valor, se cambia el prefijo en ambos servicios a la vez.

## Envío móvil de siniestros (HU-M01)

Contratos: [BFF móvil](openapi/bff-movil.yaml), [siniestros](openapi/siniestros.yaml)
y [evento SiniestroReportado v1](events/SiniestroReportado.v1.schema.json).
El borrador no es un reporte definitivo. Flujo para Android:

1. `POST /v1/siniestros/borradores` con `polizaId`, `descripcion` e `Idempotency-Key` UUID.
2. `PATCH /v1/siniestros/borradores/{borradorId}` completa/corrige `descripcion`, `causa`
   y `fechaEvento`. `causa` es el **código de cobertura/amparo**, no texto libre.
   `fechaEvento` es un instante explícito con zona, por ejemplo `2026-11-17T10:00:00-05:00`;
   no se sustituye por la fecha del envío. No puede ser futura ni estar fuera de la
   vigencia de la póliza (fechas de vigencia evaluadas en Bogotá).
   `causa` y `fechaEvento` pueden borrarse con `null`, pero deben completarse al enviar.
3. Agregar, cargar y confirmar **al menos una evidencia**. Todas las incluidas deben
   estar `CARGADA`. La confirmación verifica SHA-256, tamaño y MIME y conserva el
   objeto fuera de `tmp/`; la limpieza temporal no elimina esa copia.
   `DELETE .../{borradorId}/evidencias/{evidenciaId}` quita una referencia mientras
   el borrador siga abierto; no elimina físicamente el archivo.
4. `POST /v1/siniestros/borradores/{borradorId}/envio`, con `Idempotency-Key` UUID y
   sin cuerpo o `{}`. No admite datos adicionales; se envía la versión guardada.
   La póliza propia debe seguir `ACTIVA` y vigente. Devuelve **201**, también en reintentos:

   ```json
   {
     "borradorId": "<uuid>",
     "siniestroId": "<uuid>",
     "codigo": "88-2026-1000",
     "estado": "REPORTADO",
     "reportadoEn": "2026-11-18T15:00:00Z"
   }
   ```

El siniestro `MOVIL`, evidencias, historial, evento de outbox, cierre del borrador y
resultado idempotente se guardan en una transacción. Dos envíos concurrentes, incluso
con claves distintas, recuperan el mismo caso. Reusar una clave para otro borrador
produce `422 IDEMPOTENCIA_CONFLICTO`. La clave identifica la operación sobre el borrador;
los datos se corrigen por PATCH antes del primer envío confirmado.

Ante un error se conserva el mismo borrador y sus evidencias confirmadas. Tras un
timeout, repetir el envío con la misma clave o consultar el borrador: `estado=CONVERTIDO`
y `envio` recuperan la confirmación, aunque la póliza haya vencido después.
Los convertidos desaparecen de `GET /v1/siniestros/borradores`.
Las cargas no confirmadas siguen siendo temporales y pueden requerir renovar su URL y recargar.

`GET /v1/siniestros?limite=20&cursor=...` lista definitivos del titular, y
`GET /v1/siniestros/{siniestroId}` muestra estado **actual**, datos, evidencias e historial.
El envío conserva su respuesta original `REPORTADO`, aunque el analista ya haya tomado el caso.
El BFF obtiene el cliente del token; las rutas internas `/v1/clientes/{clienteId}/siniestros`
confían en ese cliente resuelto por el BFF, como las consultas internas de pólizas.
No se deben exponer esas rutas internas directamente al móvil.

Los errores incluyen código estable, `correlationId` y `detalles.campos` cuando aplica.
Falta de datos, causa no cubierta, fecha inválida o ausencia de evidencias: `422 SOLICITUD_INVALIDA`.
Ver también [códigos de error](codigos-error.md).
La consulta de pólizas no informa sumas por amparo: se representan con `null` en el
detalle del analista (también en BFF web), sin inventar montos ni beneficiarios.
La aprobación monetaria permanece bloqueada si faltan esos datos.

### Adjuntar ubicacion al reporte movil (F-M1.1)

La ubicacion es metadato del **reporte**, no del archivo de evidencia. Se agrega al
mismo borrador mediante `PATCH /v1/siniestros/borradores/{borradorId}` con el token
del cliente, sin `clienteId`:

```json
{
  "ubicacion": {
    "permiso": "PRECISO",
    "lat": 4.60971,
    "lon": -74.08175,
    "precisionM": 35.5,
    "capturadaEn": "2026-11-18T10:00:00-05:00"
  }
}
```

`lat` y `lon` son coordenadas WGS84 en grados (rangos inclusivos -90..90 y
-180..180). `precisionM` es la precision horizontal real informada por el proveedor,
en metros, finita y mayor que cero; no se calcula por decimales de las coordenadas.
`capturadaEn` es la fecha de la lectura **con zona horaria**, no sustituye
`fechaEvento`. No se modifica EXIF, contenido, checksum ni metadatos de S3.

El permiso declarado admite `PRECISO`, `APROXIMADO`, `DENEGADO`,
`DENEGADO_PERMANENTEMENTE` y `NO_SOLICITADO`. Solo los dos primeros admiten
coordenadas. Si se adjunta una lectura, deben enviarse juntos latitud, longitud,
precision y fecha. No se aceptan valores infinitos, NaN, booleanos ni lecturas
parciales. El backend no puede comprobar el permiso del SO: Android debe obtener
consentimiento antes de leer o enviar coordenadas.

Para registrar rechazo, permiso permanente denegado o ausencia de lectura:

```json
{"ubicacion": {"permiso": "DENEGADO"}}
```

Enviar `PRECISO` o `APROXIMADO` sin lectura permite registrar que el permiso existe
pero el proveedor no entrego ubicacion. Este objeto **reemplaza** la ubicacion
anterior y elimina sus coordenadas; omitir `ubicacion` conserva lo guardado.
`ubicacion: null` no se admite: la ausencia debe ser explicita mediante el permiso.

El PATCH devuelve `200` con el borrador y `ubicacion`, incluyendo el `estado`
calculado por el servidor (no enviarlo en la solicitud):

- `DISPONIBLE`: lectura autorizada con `precisionM <= ubicacionPrecisionMaxM`.
- `NO_CONFIABLE`: lectura completa que supera ese maximo. Se guarda sin redondear
  ni perder coordenadas, precision o fecha; Android debe marcarla y ofrecer reintento.
- `NO_DISPONIBLE`: no hay lectura. Las coordenadas, precision y fecha son `null`.

Los GET de borrador, pendientes y detalle del siniestro definitivo conservan esos
metadatos. `ubicacion: null` en datos anteriores significa que no se registro este
contexto; nunca se inventa una ubicacion ni un permiso para los casos existentes.

#### Regla por tipo y configuracion

`GET /v1/siniestros/limites` conserva los limites de evidencia y agrega:

```json
{
  "ubicacionObligatoria": true,
  "ubicacionTiposOpcionales": ["AUXILIO_FUNERARIO", "ENFERMEDADES_GRAVES"],
  "ubicacionPrecisionMaxM": 100.0
}
```

**AUXILIO_FUNERARIO y ENFERMEDADES_GRAVES permiten enviar sin ubicacion**;
los demas tipos requieren ubicacion confiable. Para esta regla, el tipo es `causa`,
el codigo exacto de cobertura/amparo de la poliza.
Android debe consultar los limites y considerar obligatoria la ubicacion solo si
`ubicacionObligatoria` es `true` y la causa no esta en `ubicacionTiposOpcionales`.
Deshabilitar el boton es una ayuda UX: el servidor aplica la misma regla al enviar.

Configuracion exclusiva de `siniestros` (tambien admite prefijo `SOLVENTA_`):

- `UBICACION_OBLIGATORIA=true`: valor global por defecto.
- `UBICACION_TIPOS_OPCIONALES='["AUXILIO_FUNERARIO","ENFERMEDADES_GRAVES"]'`:
  excepciones por codigo de causa, con esos dos valores por defecto.
  No requiere modificar codigo para cambiar excepciones; `[]` vuelve a exigir
  ubicacion en todos los tipos. La obligatoriedad de una cobertura en el producto
  no determina la obligatoriedad de ubicacion.
- `UBICACION_PRECISION_MAX_M=100`: umbral positivo configurable. **100 m es una
  decision inicial de producto, no un estandar universal**. El limite es inclusivo;
  una lectura aproximada se conserva con su precision y usa la misma regla.

En AWS se pueden sobrescribir estas variables mediante
`service_environment.siniestros` de `infra/apps`. Los limites del BFF se obtienen
del servicio, sin duplicar configuracion en Android ni en el BFF.

Si la ubicacion es obligatoria y falta o no es confiable, el envio responde
`422 SOLICITUD_INVALIDA`, con `detalles.campos: ["ubicacion"]` y `correlationId`.
El borrador queda abierto y conserva sus evidencias; corregir por PATCH y reintentar
el envio del **mismo borrador y clave idempotente**. Si es opcional, puede enviarse
con estado no disponible o no confiable, sin falsear el contexto.
Coordenadas sin permiso, fecha sin zona, lectura incompleta, rangos invalidos,
`ubicacion: null` o un `estado` enviado por Android tambien producen `422`.
Se mantienen `401`, `403`, `404 BORRADOR_NO_ENCONTRADO`, `409 BORRADOR_CONVERTIDO`
y `503` de las rutas existentes. Otro titular no puede leer ni modificar la ubicacion.

La conversion copia los metadatos a `siniestros.ubicacion_declarada` (JSONB existente)
en la misma transaccion del reporte. El envio revalida el umbral vigente, incluso
dentro de la transaccion, y guarda la confianza recalculada tanto en el borrador
convertido como en el definitivo. Una confirmacion previa se recupera sin revalidar
ubicacion, poliza ni archivos. Los eventos/outbox no incorporan coordenadas.

#### Privacidad, despliegue y trabajo Android pendiente

- RDS ya usa `storage_encrypted=true` y KMS en `infra/platform/database.tf`;
  la nueva columna y el JSONB definitivo quedan cubiertos por ese cifrado en reposo.
- `sanitizar` y el enmascarado de texto redactan `lat`, `lon` y sus nombres
  alternativos en logs. Los errores de validacion incluyen campos, nunca valores.
  Android tampoco debe registrar coordenadas en logs, analitica o reportes de errores.
- Desplegar **siniestros,bff-movil**, alcance **microservicios** del workflow
  **Infra - Desplegar**, con pruebas habilitadas. Incluye la configuracion de apps.
  La migracion **007** se aplica al arrancar: agrega `borradores.ubicacion` y reutiliza
  el JSONB definitivo; no requiere recrear RDS ni desplegar bases/plataforma.
  La reversa rechaza eliminar metadatos ya guardados.
- **Cambio de comportamiento intencional:** un borrador abierto anterior sin ubicacion
  debe completarse antes de enviarse mientras su causa sea obligatoria. Coordinar
  este despliegue con Android. Los siniestros ya enviados no se invalidan.
- Fuera de este repositorio quedan Play Services Location y su dependencia en
  `libs.versions.toml`, permisos y explicacion segun mockups, boton y reintento,
  visores de precision/fecha y ausencia, Robolectric/WireMock, Compose/Espresso,
  pruebas fisicas exterior/interior, TalkBack, fuente al 200 %, es-CO y en-US.
  No se verificaron esos criterios de UI en el backend.

#### Semillas para ejecutar R4 en int/local

`consulta` agrega las polizas de `services/consulta/src/consulta/adapters/outbound/semillas/polizas_semilla.json`
al arrancar con `CONSULTA_SEMILLA_POLIZAS=true` (ya habilitado en `int`).
Los clientes C01 y C28-C33 ya existen con `CLIENTES_SEMILLA_SINTETICA=true`.
No se crean documentos nuevos, usuarios Cognito, contrasenas, evidencias ni siniestros
definitivos: iniciar sesion con una cuenta vinculada al cliente elegido y completar
el reporte desde Android. Los documentos siguientes son **100 % sinteticos**.

Las polizas 001-004 se conservan sin cambios; 005-015 son nuevas y se insertan
idempotentemente, sin duplicar ni sobrescribir datos del ambiente. Son proyecciones
QA de consulta, no emisiones reales ni disparadores de la automatizacion parametrica.
La lectura no exige consultar el catalogo para recuperar estas polizas.

**Matriz de polizas** (vigencia evaluada al 19/11/2026):

| Cliente / documento CC | Numero de poliza | Amparos disponibles | Caso de prueba |
|---|---|---|---|
| C01 / `9000000001` | `900-2026-000003` | `MUERTE` | Ubicacion obligatoria (original) |
| C01 / `9000000001` | `900-2026-000005` | `AUXILIO_FUNERARIO` | AC2: rechazar permiso y enviar |
| C01 / `9000000001` | `900-2026-000006` | `ENFERMEDADES_GRAVES` | AC2: rechazar permiso y enviar |
| C28 / `9000000028` | `900-2026-000007` | `MUERTE`, `INCAPACIDAD_TOTAL_PERMANENTE` | AC1 y AC3: ubicacion obligatoria |
| C29 / `9000000029` | `900-2026-000008` | `INCAPACIDAD` | Codigo legado: tambien exige ubicacion |
| C30 / `9000000030` | `900-2026-000009` | `INCENDIO`, `HURTO` | Ambos exigen ubicacion, incluso si la cobertura es opcional en el producto |
| C31 / `9000000031` | `900-2026-000010` | `LLUVIA_ACUMULADA` | Exige ubicacion; prueba de reporte movil, no de disparador automatico |
| C32 / `9000000032` | `900-2026-000011` | `MUERTE`, `INCAPACIDAD_TOTAL_PERMANENTE`, `ENFERMEDADES_GRAVES`, `AUXILIO_FUNERARIO` | Cambiar de amparo opcional a obligatorio y viceversa en el mismo borrador |
| C33 / `9000000033` | `900-2026-000012` | `ENFERMEDADES_GRAVES`, `AUXILIO_FUNERARIO` | Probar ambos amparos opcionales con otro titular |
| C01 / `9000000001` | `900-2025-000013` | `AUXILIO_FUNERARIO` | Vencida: la excepcion de ubicacion no permite reportar |
| C01 / `9000000001` | `900-2026-000014` | `ENFERMEDADES_GRAVES` | No iniciada: la excepcion de ubicacion no permite reportar |
| Movil / `1020456789` | `900-2026-000015` | `MUERTE`, `INCAPACIDAD_TOTAL_PERMANENTE`, `ENFERMEDADES_GRAVES`, `AUXILIO_FUNERARIO` | Mismos cambios de amparo para el cliente movil preexistente |

El cliente movil conserva ademas `900-2026-000001` (`MUERTE`, `INCAPACIDAD`,
vigente) y `900-2025-000002` (`MUERTE`, vencida). C02 / CC `9000000002`
conserva `900-2025-000004` (`INCENDIO`, `HURTO`), cuya vigencia termina
**20/11/2026**; para pruebas posteriores usar C30.

Obtener el `polizaId` desde `GET /v1/polizas` con el token del titular y abrir su
detalle para elegir `causa` desde las coberturas. Como referencia, el UUID de
`900-2026-000005` es `5eed0000-0000-4000-8000-000000000005`; los demas siguen
el mismo prefijo y sufijo de 12 digitos de la secuencia 001-015. Los UUID de cliente
C28-C33 usan sufijos hexadecimales: no deducirlos concatenando el documento.

Las nuevas polizas vigentes cubren 15/10/2026-14/10/2027, excepto lluvia,
01/11/2026-31/10/2027. La 013 termino el 30/09/2026 y la 014 inicia el
01/01/2027. Son fechas fijas, no se renuevan al reiniciar. Todas se almacenan
como `ACTIVA`: `vigente` se calcula con las fechas. La proyeccion actual solo
admite ese estado; no se fabrican polizas `CANCELADA` o `EMITIDA` mediante esta
semilla. Sus rechazos se cubren con pruebas automatizadas de validacion.

**Ejecucion y resultados esperados** (en todos los envios positivos: descripcion,
causa incluida en la poliza, fecha real pasada dentro de vigencia y al menos una
evidencia cargada/confirmada):

| Escenario | Datos / accion | Resultado |
|---|---|---|
| AC1 | C28, poliza 007; permiso `PRECISO`, lectura completa de 35.5 m | PATCH 200 `DISPONIBLE`; envio 201 `REPORTADO` |
| AC2 | C01, 005 y 006; repetir con `DENEGADO` y `DENEGADO_PERMANENTEMENTE` sin coordenadas | PATCH 200 y envio 201; detalle conserva `NO_DISPONIBLE`, permiso y campos de lectura `null` |
| Falta de permiso en obligatorio | C28/C29/C30/C31; denegar permiso o no tener lectura | Borrador recuperable; envio 422 con campo `ubicacion` |
| Precision en el limite | C28: 99.99, 100 y 100.01 m con umbral 100 | Los dos primeros aceptan envio; el ultimo guarda `NO_CONFIABLE` y bloquea con 422 |
| Permiso aproximado | C28, permiso `APROXIMADO`, precision real del proveedor | La misma comparacion inclusiva; no inventar ni redondear precision |
| Precision mala en opcional | C01, 005/006, lectura autorizada por encima de 100 m | Guarda `NO_CONFIABLE` y permite envio, conservando el contexto |
| Proveedor sin lectura | C01 005/006, permiso concedido sin los cuatro datos de lectura | `NO_DISPONIBLE`; permite envio por ser opcional |
| Cambio de causa | C32 011 o movil 015: guardar rechazo con amparo opcional, cambiar a `MUERTE` antes de enviar | Bloquea con 422 hasta adjuntar ubicacion confiable; cambiar de nuevo a amparo opcional permite enviar |
| Vigencia | C01 013 y 014 | `vigente=false`, selector no las ofrece como elegibles; POST forzado de borrador responde 409 `POLIZA_NO_VIGENTE` |
| Titularidad | Sesion C01 intenta usar la poliza 007 de C28, y viceversa | 403 `POLIZA_NO_AUTORIZADA`, sin datos de la poliza ajena |
| Amparo ajeno a la poliza | C01 005, guardar `causa=MUERTE` | Envio 422 con campo `causa`, sin crear caso |
| Consentimiento incoherente | Enviar coordenadas con `DENEGADO`, lectura parcial o `ubicacion:null` | PATCH 422; no modifica lo guardado |
| Carga incompleta | C01 005/006 con evidencia pendiente | Envio 409 `EVIDENCIA_NO_CARGADA`; no se elude por ser ubicacion opcional |
| Recuperacion / idempotencia | C01 005/006: corregir mismo borrador, reintentar tras timeout o enviar simultaneamente | Un solo siniestro y misma confirmacion tras registrar; sin recrear borrador ni duplicar archivos |
| Sesion invalida | Cualquier poliza, sin token valido | 401, sin cambios |

AC4, TalkBack, fuente al 200 %, es-CO/en-US y lecturas fisicas exterior/interior
se ejecutan en Android con estos mismos datos; no se validan con la semilla por si sola.
No se registran coordenadas ni enlaces firmados en evidencias de prueba compartidas.

**Despliegue de esta ampliacion:** workflow **Infra - Desplegar**, rama de la
entrega, `ambiente=int`, `alcance=microservicios`,
`servicios=consulta,siniestros,bff-movil`, `probar=true`. Reconstruir `consulta`
es necesario para que cargue el JSON nuevo; desplegar solo aplicaciones no incorpora
el archivo a la imagen. Se aplican las excepciones de ubicacion de `infra/apps`.
No requiere modificar plataforma ni bases; si aun no estaba aplicada, la migracion
007 de siniestros se ejecuta al arrancar. La siembra de clientes existente debe estar
habilitada y las sesiones deben corresponder a los titulares de la matriz.

### Descargar evidencias desde Android

`POST /v1/siniestros/{siniestroId}/evidencias/{evidenciaId}/enlace`,
con `Authorization: Bearer <token>` y **sin cuerpo**:

```json
{
  "url": "https://bucket.s3.amazonaws.com/...",
  "expiraEn": "2026-11-18T21:05:00Z",
  "nombreArchivo": "evidencia.jpg",
  "tipoContenido": "image/jpeg"
}
```

Responde `200`. El BFF resuelve el titular desde el token y `siniestros` comprueba
la propiedad del caso, pertenencia de la evidencia y confirmación de la carga.
Una evidencia definitiva sin borrador asociado sigue siendo una evidencia confirmada
(casos previos/portal); si tiene borrador, se exige la confirmación correspondiente.
Se verifica existencia e integridad antes de firmar. El enlace es **HTTPS**, dura como
máximo **300 segundos** (`EVIDENCIA_URL_TTL_S`) y fija la versión del objeto cuando S3
la informa. El archivo se entrega con `Content-Type` y `Content-Disposition: attachment`,
incluyendo nombre UTF-8. No se publica el objeto ni se guardan URLs en logs/outbox.

Android debe pedir el enlace al abrir/descargar el archivo y descargar directamente
desde esa URL **sin reenviar el token de Cognito a S3**. No persistir ni registrar
la URL firmada. Si vence, solicitar una nueva; no usar el detalle como fuente de enlaces.
En local el endpoint público de firma también debe ser HTTPS (LocalStack admite TLS).

- `401`: sesión inválida.
- `404 SINIESTRO_NO_ENCONTRADO`: inexistente o de otro titular, sin revelar cuál.
- `404 EVIDENCIA_NO_ENCONTRADA`: no existe o no corresponde al caso.
- `404 EVIDENCIA_NO_DISPONIBLE`: archivo ausente.
- `409 EVIDENCIA_NO_CARGADA`: falta confirmación.
- `422 INTEGRIDAD_EVIDENCIA_INVALIDA`: checksum distinto; no se firma.
- `503 SERVICIO_NO_DISPONIBLE`: falla de servicio, permisos S3 o almacenamiento.

La ruta interna agrega `/v1/clientes/{clienteId}` y solo la invoca el BFF con el
cliente resuelto, como las consultas móviles. No debe exponerse directamente.
Esta ampliación requiere desplegar **bff-movil y siniestros**, incluyendo el cambio
IAM de `infra/apps` descrito abajo. No requiere migraciones ni cambios en BFF web.

### Despliegue

Workflow **Infra - Desplegar**, ambiente `int`, alcance `microservicios`,
servicios `siniestros,bff-movil,bff-web`, `probar=true`. La migración `006` se aplica
al arrancar siniestros; no requiere desplegar plataforma ni bases.
Esperar a que los tres servicios estén estables antes de probar Android.
Para la descarga se agrega a `siniestros` `s3:ListBucket` condicionado exclusivamente
al prefijo `siniestros/*`. Permite consultar la clave exacta si HEAD devuelve `403`
ambiguo y distinguir ausencia (`404`) de permisos/almacenamiento (`503`).
No permite listar otros prefijos ni borrar archivos. `microservicios` aplica
`infra/apps`, por lo que incluye este permiso sin desplegar plataforma ni bases.
No revertir la migración si existen borradores convertidos: su reversa lo rechaza
para conservar las confirmaciones y evitar reaperturas.

La conservación S3 tiene corte independiente `S3_CONSERVACION_TIMEOUT_MS=2000`;
la lectura del detalle mantiene `S3_TIMEOUT_MS=300`.
Confirmación y envío en el BFF usan `SOLVENTA_SINIESTROS_ESCRITURA_TIMEOUT_MS=15000`,
sin ampliar el corte de las consultas. Los timeouts no reintentan automáticamente:
el móvil recupera la confirmación o reintenta el mismo borrador.

## Datos sintéticos

Todos los datos son **100 % sintéticos**: Solventa no usa datos reales de clientes ni proveedores reales. El simulador sustituye a Open Finance y a Datos Abiertos, y así las pruebas, los logs y las trazas nunca exponen información personal o financiera de nadie. Los identificadores de perfilamiento siguen el patrón `00000000-0000-4000-8000-0000000000NN`; los `consentimientoId` del stub, `00000000-0000-4000-9000-0000000000NN`.

En `simulador.<FUENTE>.datos`, una clave con punto (`listasRestrictivas.coincidencias`) es una ruta anidada; con el comportamiento `incompleta`, las claves listadas se **eliminan** de la respuesta nominal.
