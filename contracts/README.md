# Contratos

Contratos que publica el backend para sus consumidores (web, móvil, socios y otros servicios).

## Convención

- **Generados desde el código.** Cada OpenAPI de `openapi/<servicio>.yaml` se genera con `app.openapi()` y una prueba de contrato (`services/<servicio>/tests/contract/test_openapi.py`) verifica que el archivo publicado sea igual a lo que sirve la app. Los esquemas de eventos y del modelo de riesgo se generan desde Pydantic (`TypeAdapter(...).json_schema()`) y también se verifican con una prueba.
- Un cambio de contrato se hace primero aquí; el frontend lo copia con `scripts/sync-contracts.sh <ref>` desde un *commit* de `main`.

## Archivos

| Archivo | Contenido | Dueño | Cómo se regenera |
| --- | --- | --- | --- |
| `openapi/bff-web.yaml` | API de `bff-web`; incluye la lectura de consentimientos del asesor (`GET /v1/clientes/{clienteId}/consentimientos` y `.../{consentimientoId}/historial`, HU-W31, PR 3.5) y, desde HU-W13, la suscripción: `POST /v1/suscripciones`, `GET /v1/suscripciones?cotizacionId=` y `GET /v1/suscripciones/{decisionId}` (grupo `asesores`; la lectura también `suscriptores`), `GET /v1/bandeja-suscripcion` y `.../resumen` (grupo `suscriptores`). Sus DTO son copia de `openapi/polizas.yaml`; las vistas (`DecisionSuscripcionVista`, `CasoBandejaVista`) agregan `clienteNombre` desde `clientes` | Por confirmar (responsable de `bff-web`) | Ver [Regenerar un contrato](#regenerar-un-contrato) |
| `openapi/api-socios.yaml` | API de `api-socios`: `/admin/socios` (solo `bff-web`; alta, plan y consumo) y rutas del socio (HU-W01, HU-W02). Desde HU-W04, `POST /cotizaciones` y `GET /cotizaciones/{cotizacion_id}` con la oferta pública v1 (`OfertaV1`, lista blanca de campos; 410 `OFERTA_VENCIDA`) | Integrante 1 (`api-socios`) | Ver [Regenerar un contrato](#regenerar-un-contrato) |
| `openapi/publicados/api-socios.v1.yaml` | **Versión v1 publicada a los socios, congelada** (HU-W04 AC2): las operaciones del socio ya disponibles (`/socio/me` y `/cotizaciones`), sin `/admin`, `/health` ni las que aún responden 501. `services/api-socios/tests/contract/test_contrato_publicado.py` falla si el contrato actual elimina o renombra un campo, cambia su tipo, vuelve obligatorio un campo opcional de la solicitud, deja de garantizar un campo de la respuesta, elimina una operación, un código o un valor de un enum. Un cambio incompatible es una versión nueva (v2), no una edición de este archivo | Integrante 1 (`api-socios`) | Solo al publicar una versión: `uv run python services/api-socios/tests/contract/test_contrato_publicado.py` |
| `pact/socio-referencia-api-socios.json` | Pacto de un **socio de referencia** (consumidor) → `api-socios` (HU-W04 AC2): cotiza, consulta y presenta la oferta con solo los campos publicados (matchers de tipo y formato), recotiza ante `410 OFERTA_VENCIDA` y recibe `404 OFERTA_NO_ENCONTRADA` por la oferta de otro socio. Las solicitudes son las que llegan a `api-socios` (con los `X-Partner-*` que agrega API Gateway) | Integrante 1 (`api-socios`) | `make test-contract SERVICE=api-socios` lo genera (`test_pact_socio_referencia.py`) y lo verifica (`test_pact_socio_referencia_proveedor.py`) |
| `pact/api-socios-cotizacion.json` | Pacto consumidor `api-socios` → proveedor `cotizacion` (HU-W04): `POST /v1/cotizaciones` y `GET /v1/cotizaciones/{cotizacionId}` con el actor `SOCIO`, la oferta (`productoId` y `oferta`; lector tolerante), `404 COTIZACION_NO_ENCONTRADA` de la cotización de otro socio y `422 PRODUCTO_FUERA_DE_VIGENCIA`. `cotizacion` lo verifica con su app real y los puertos en memoria (`services/cotizacion/tests/contract/test_pact_api_socios.py`) | Integrante 1 (`api-socios`) | `make test-contract SERVICE=api-socios` lo genera; `make test-contract SERVICE=cotizacion` lo verifica |
| `pact/bff-web-api-socios.json` | Pacto consumidor `bff-web` → proveedor `api-socios` (HU-W01, HU-W02) | Integrante 1 | `make test-contract SERVICE=bff-web` lo genera; `make test-contract SERVICE=api-socios` lo verifica |
| `openapi/bff-movil.yaml` | API de `bff-movil` para la app móvil: sesión, verificación de identidad y registro (HU-M06); `/v1/consentimientos` (otorgar y listar), `/v1/consentimientos/{consentimientoId}` y `/v1/consentimientos/texto-vigente` (HU-M08); `/v1/consentimientos/{consentimientoId}/revocacion` y `.../historial` (HU-M09). El titular sale siempre del token | Por confirmar (responsable de `bff-movil`) | Ver [Regenerar un contrato](#regenerar-un-contrato) |
| `pact/bff-movil-clientes.json` | Pacto consumidor `bff-movil` → proveedor `clientes`: rutas de consentimiento (HU-M08) y su revocación e historial (HU-M09). Se coteja con `openapi/clientes.yaml`; la verificación del lado de `clientes` está pendiente | Por confirmar (responsable de `bff-movil`) | `make test-contract SERVICE=bff-movil` lo genera |
| `pact/cotizacion-clientes.json` | Pacto consumidor `cotizacion` → proveedor `clientes`: verificación del consentimiento (`POST /v1/clientes/{clienteId}/consentimientos/verificaciones`) con cada decisión y causa, y `404 CLIENTE_NO_ENCONTRADO` (HU-W31). Se coteja con `openapi/clientes.yaml`; `clientes` lo verifica como proveedor contra Postgres real (`services/clientes/tests/contract/test_pact_proveedor.py`, `make test-contract SERVICE=clientes`, requiere Docker) | Por confirmar (responsable de `cotizacion`) | `make test-contract SERVICE=cotizacion` lo genera |
| `pact/polizas-cotizacion.json` | Pacto consumidor `polizas` → proveedor `cotizacion`: insumos de la oferta para la suscripción (`GET /v1/cotizaciones/{cotizacionId}/insumos-suscripcion` con el actor `ASESOR`), con perfil, sin perfil y `404 COTIZACION_NO_ENCONTRADA` (HU-W13, CT-W13-02). `cotizacion` lo verifica como proveedor con su app real y los puertos en memoria (`services/cotizacion/tests/contract/test_pact_polizas.py`) | @capbmaster | `make test-contract SERVICE=polizas` lo genera; `make test-contract SERVICE=cotizacion` lo verifica |
| `openapi/adaptador-datos.yaml` | API `/v1/consultas` de `adaptador-datos` (HU-W10) | @capbmaster | Ver [Regenerar un contrato](#regenerar-un-contrato) |
| `openapi/adaptador-identidad.yaml` | API `/v1/verificaciones` de `adaptador-identidad`: verifica la selfie y la prueba de vida con el proveedor KYC (HU-M06) | Por confirmar (responsable de `adaptador-identidad`) | Ver [Regenerar un contrato](#regenerar-un-contrato) |
| `openapi/clientes.yaml` | API de `clientes`: `/v1/clientes/actual`, `/v1/clientes/{clienteId}/identidad` y `/v1/clientes/actual/identidad/verificaciones` (HU-M06); `/v1/clientes/actual/consentimientos` (otorgar y listar), `/v1/clientes/actual/consentimientos/{consentimientoId}` y `/v1/consentimientos/textos/vigente` (HU-M08); `.../{consentimientoId}/revocacion` y `.../{consentimientoId}/historial` (HU-M09); `/v1/clientes/{clienteId}/consentimientos/verificaciones` (HU-W31); lectura web para el asesor (`X-Actor-Rol=ASESOR`, solo `GET`): `/v1/clientes/{clienteId}/consentimientos` y `.../{consentimientoId}/historial` (HU-W31, PR 3.3). `cotizacion` coteja sus DTO de identidad y verificación con este archivo (`services/cotizacion/tests/contract/test_dto_clientes.py`); `bff-web` coteja los de alta, identidad y lectura de consentimientos (`services/bff-web/tests/contract/test_openapi.py`) | Por confirmar (responsable de `clientes`) | Ver [Regenerar un contrato](#regenerar-un-contrato) |
| `events/ConsentimientoOtorgado.v1.schema.json` | Evento `ConsentimientoOtorgado` v1 del outbox de `clientes` (HU-M08); `aggregateVersion` es opcional y vale 1 | Por confirmar (responsable de `clientes`) | `uv run python -m clientes.adapters.outbound.eventos` (desde la raíz) |
| `events/ConsentimientoRevocado.v1.schema.json` | Evento `ConsentimientoRevocado` v1 del outbox de `clientes` (HU-M09); `aggregateVersion` es opcional y vale 2 | Por confirmar (responsable de `clientes`) | `uv run python -m clientes.adapters.outbound.eventos` (desde la raíz) |
| `events/VerificacionConsentimientoRegistrada.v1.schema.json` | Evento `VerificacionConsentimientoRegistrada` v1 del outbox de `clientes` (HU-W31): una fila por verificación; `aggregateId` = `clienteId`, sin `aggregateVersion`; solo para `auditoria` (sin consumidor hasta la PR 2.2 y el mapeo de la 3.3) | Por confirmar (responsable de `clientes`) | `uv run python -m clientes.adapters.outbound.eventos` (desde la raíz) |
| `openapi/catalogo.yaml` | API interna de `catalogo`: `/v1/modelos-riesgo` (HU-W11), `/v1/productos/{productoId}`, `.../reglas-prima/vigente?fecha=` y `.../reglas-prima/{version}` (HU-W03), y `.../reglas-suscripcion/vigente?fecha=` y `.../reglas-suscripcion/{version}` (HU-W13). La consumen `cotizacion` y `polizas` | @capbmaster (modelos y reglas de suscripción); HU-W03 (productos) | Ver [Regenerar un contrato](#regenerar-un-contrato) |
| `openapi/cotizacion.yaml` | API interna de `cotizacion`: `/v1/perfiles` (HU-W10 a HU-W31) y, desde HU-W03, `POST /v1/cotizaciones`, `GET /v1/cotizaciones/{cotizacionId}` y `GET /v1/productos/{productoId}/condiciones` (actores `ASESOR` y `SOCIO`). Desde HU-W13, `GET /v1/cotizaciones/{cotizacionId}/insumos-suscripcion` (actor `ASESOR`, lo consume `polizas`) y el campo opcional `vigenteHasta` de `CotizacionRespuesta` (DS2-20). `bff-web` copia sus DTO y `tests/contract/test_openapi.py` de `bff-web` verifica que sean iguales. Desde HU-W04, el campo opcional `oferta` de `CotizacionRespuesta` (`OfertaDto`: código, precio, coberturas con su suma, exclusiones, vigencia y estado al consultar) y `ofertaId` y `codigo` llenos en los insumos de suscripción | Por confirmar (responsable de `cotizacion`) | Ver [Regenerar un contrato](#regenerar-un-contrato) |
| `modelos/reglas-prima.v1.schema.json` | Esquema de la definición de las reglas de prima (HU-W03): tasa base, factores por nivel (`BAJO`, `MEDIO`, `ALTO`, `SIN_PERFIL`) y por variable, recargos por cobertura opcional, prima mínima y máxima, redondeo (escala y modo) y límites. Decimales como texto. Desde HU-W04, el bloque opcional `oferta` (`vigenciaMinutos`, `exclusiones` y `sumasCoberturas`), que no se serializa cuando falta: el hash de las reglas anteriores no cambia. `cotizacion` tiene una copia (`DefinicionReglasPrimaCotizacion`) y CT-W03-01 verifica que sea igual | HU-W03, HU-W04 | `uv run python -m catalogo.adapters.inbound.http.esquema_prima > contracts/modelos/reglas-prima.v1.schema.json` |
| `openapi/polizas.yaml` | API interna de `polizas` (HU-W13): `POST /v1/suscripciones` (actor `ASESOR`, `Idempotency-Key`), `GET /v1/suscripciones?cotizacionId=`, `GET /v1/suscripciones/{decisionId}` y `.../reconstruccion` (`ASESOR` o `SUSCRIPTOR`; las reglas `RESERVADA` llegan sin `valorCaso` para `ASESOR`, DS2-07), `GET /v1/suscripciones/bandeja` y `.../resumen` (`SUSCRIPTOR`). Desde HU-W14, la decisión del `POST` y del `GET /v1/suscripciones/{decisionId}` trae el campo opcional `comunicacion` (`ComunicacionDto`: condición, si admite contratación, siguiente paso, vigencia, motivo para el cliente, alternativas y plazo de respuesta), derivado al leer y sin códigos ni valores de reglas. La consume `bff-web` | @capbmaster (suscripción); HU-W15 y HU-W17 agregarán aceptación y emisión | Ver [Regenerar un contrato](#regenerar-un-contrato) |
| `modelos/reglas-suscripcion.v1.schema.json` | Esquema de la definición de las reglas de suscripción (HU-W13): producto, alternativas informativas (E03) y reglas `{codigo SUS-NNN, orden, tipo EXCLUYENTE\|REVISION, descripcion (clave de texto), campo, operador, valor, visibilidad EXPLICABLE\|RESERVADA, motivoCliente}`. `campo`, `operador` y `motivoCliente` son conjuntos cerrados; los valores numéricos van como texto decimal | @capbmaster | `uv run python -m catalogo.adapters.inbound.http.esquema_suscripcion > contracts/modelos/reglas-suscripcion.v1.schema.json` |
| `events/DecisionSuscripcionRegistrada.v1.schema.json` | Evento `DecisionSuscripcionRegistrada` v1 del outbox de `polizas` (HU-W13): una fila por decisión, en la misma transacción; `aggregateId` = `decisionId`. Sin montos ni datos personales: resultado, reglas, versión y huellas. Para `auditoria` | @capbmaster | `uv run python -m polizas.adapters.outbound.eventos` (desde la raíz) |
| `datos-sinteticos/cotizaciones-suscripcion.json` | Cotizaciones sintéticas S01–S11 para la suscripción (HU-W13): aprobada, rechazadas (edad 68, plazo 300, listas restrictivas), revisiones (suma y confianza, suma, perfil insuficiente, sin perfil), oferta vencida e identidad no verificada o pendiente. Desde HU-W04, S01 y S08 (los casos del pacto `polizas-cotizacion`) traen `ofertaId` y `codigo`; los demás siguen en `null`, como una cotización anterior a la oferta. Cada caso trae los `insumos` que usan los dobles de `polizas`, cómo crearlo en local sobre un cliente de `clientes-perfilamiento.json` y el resultado esperado | @capbmaster | A mano |
| `alcances-perfilamiento.json` | Propósito, alcances (fuentes), tipo, textos es-CO / en-US y campos canónicos | @capbmaster | A mano |
| `datos-sinteticos/clientes-perfilamiento.json` | Clientes sintéticos C01–C19: identidad, consentimiento, datos declarados, comportamiento del simulador y resultado esperado | @capbmaster | A mano. Después de cambiarlo, regenerar los mapeos de los aliados: `python services/simulador-aliados/scripts/generar_mapeos.py todo`. Las pruebas de módulo de `cotizacion` siembran `clientes` real desde este archivo (`services/cotizacion/tests/module/conftest.py`); `test_clientes_real_decide_lo_que_dicen_los_datos_sinteticos` (`make test-module SERVICE=cotizacion`) falla si la siembra no reproduce un caso |
| `codigos-error.md` | Códigos de error estables | Todos | A mano |
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

## Datos sintéticos

Todos los datos son **100 % sintéticos**: Solventa no usa datos reales de clientes ni proveedores reales. El simulador sustituye a Open Finance y a Datos Abiertos, y así las pruebas, los logs y las trazas nunca exponen información personal o financiera de nadie. Los identificadores de perfilamiento siguen el patrón `00000000-0000-4000-8000-0000000000NN`; los `consentimientoId` del stub, `00000000-0000-4000-9000-0000000000NN`.

En `simulador.<FUENTE>.datos`, una clave con punto (`listasRestrictivas.coincidencias`) es una ruta anidada; con el comportamiento `incompleta`, las claves listadas se **eliminan** de la respuesta nominal.
