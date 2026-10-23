# HU-W04 · Oferta con precio y coberturas

> Como socio de distribución quisiera que Solventa devuelva la oferta con el precio y las coberturas calculadas para presentarla al cliente dentro de mi canal. (Issue #45)

Este documento recoge el diseño, el contrato público del socio, las pruebas y la evidencia de la historia. El despliegue está en [infra/DESPLIEGUE.md](../infra/DESPLIEGUE.md) (sección 5.5 y "Pruebas E2E (N3)").

## 1. Alcance y decisiones

| Tema | Decisión | Fundamento |
| --- | --- | --- |
| Dónde vive la oferta | **Entidad `Oferta` del agregado Cotización** (1:1), en la tabla `ofertas` de la base de `cotizacion` (migración `004`, aditiva e inmutable). Se crea con la prima, en la misma transacción que la cotización | `modelo_componentes.puml` ya dice "Calcula perfil, prima y oferta" y `modelo_informacion.puml` tiene el artefacto "Cotización" en el repositorio de cotización. **Sin cambios en `docs/arquitectura`** |
| De dónde salen vigencia, exclusiones y suma por cobertura | De las **reglas de prima**: bloque opcional `oferta` (`vigenciaMinutos`, `exclusiones`, `sumasCoberturas`) en `reglas-prima.v1` y una versión nueva de las reglas, `2026.10.1` (mismas tarifas que `2026.09.1`, vigente desde 2026-10-22). No hay valores en el código | Las reglas son inmutables y tienen hash: no se puede editar `2026.09.1`. El bloque falta en las reglas anteriores y no se serializa (`exclude_if`), así que su hash publicado se sigue verificando |
| Vigencia | 7 días (10.080 minutos) en `2026.10.1`, como en los mockups C04 y C08. Semiabierta: vigente si `ahora < vigenteHasta`, el instante exacto ya está vencido. Se guarda con la oferta; el estado no se guarda: se deriva al leer con el reloj de `cotizacion` | Contrato de `vigenteHasta` fijado por HU-W13 (DS2-20) |
| Reglas sin condiciones de oferta | Vigencia de la configuración de HU-W13 (`oferta_vigencia_horas`, 96 h), sin exclusiones y con la suma completa por cobertura | Compatibilidad con las reglas y cotizaciones anteriores |
| Identificador | `id` (UUID) y `codigo` legible `COT-AAAA-NNNNN` (secuencia `ofertas_codigo_seq`, año UTC de la creación, al menos 5 dígitos), como en los mockups | El cliente ve el código; los sistemas usan el UUID |
| `OFERTA_VENCIDA` | **Mismo código y mismo detalle** que HU-W13 (`detalles.accion = "NUEVA_COTIZACION"`). **410** en `api-socios`, donde la oferta es el recurso de la ruta; **409** se queda en `polizas` (suscripción sobre otro recurso) y en `asegurar_utilizable` | Decisión del 2026-10-22 con el dueño de la historia: no se rompe a HU-W13. Mismo criterio que `PRODUCTO_NO_ENCONTRADO` (422/404) |
| `GET` de una oferta vencida al socio | **410 `OFERTA_VENCIDA`**, sin el precio vencido; `detalles` con `ofertaId`, `cotizacionId` y `vigenteHasta` | AC3 "si la vigencia terminó y el socio intenta utilizar la oferta, Solventa informa que debe cotizar nuevamente": en HU-W04 el uso del socio es recuperarla para presentarla al cliente, y presentar un precio vencido es justo lo que la regla evita ("una oferta vencida no puede aceptarse"). Con 200 + `VENCIDA` el socio tendría que interpretar el estado, y el N3 no podría verificar AC3 |
| `GET` interno (portal) | 200 con `oferta.estado = VENCIDA`: el asesor ve la cotización vencida (C08) y decide recalcular | Mockup C08 |
| `POST` que repite la clave de una oferta ya vencida | 410, como el `GET` | La misma regla del canal: al socio nunca se le entrega una oferta vencida |
| Errores de HU-W03 en el canal | Pasan con el mismo código y estado (`PRODUCTO_*`, `REGLAS_NO_VIGENTES`, `DATOS_FUERA_DE_RANGO`, `COBERTURA_NO_DISPONIBLE`, `IDEMPOTENCIA_CONFLICTO`, `SOLICITUD_INVALIDA`, `CLIENTE_NO_ENCONTRADO`); de los `detalles`, solo `campos`. Todo lo demás es 503 `SERVICIO_NO_DISPONIBLE` sin detalles | No revelar la topología interna (`catalogo.producto`, etc.) |
| Mockups | Del issue: **C04 Oferta** (portal), C06 Explicación y C09 Embebida. En el mismo archivo: **C08 Cotización vencida** (AC3) y C05 Comparar coberturas. Se implementan **C04 y C08** en el portal | C05 (tres alternativas, deducibles y carencias) no tiene datos en el modelo; C06 es explicabilidad; C07 es HU-W06 (degradación); C09 es la interfaz del socio (y muestra un contrato v2) |
| Socio de prueba en N3 | Dos socios dados de alta por la administración, no `socio-a` | Los socios de Terraform solo llegan a `/health` (no están en la base de `api-socios`) y su plan `prueba` permite 5 solicitudes al día (ver sección 6) |

## 2. La oferta en `cotizacion`

`domain/cotizacion/modelos.py` (`Oferta`, `CoberturaOfertada`, `Exclusion`, `CondicionesOferta`, `EstadoOferta`) y `domain/cotizacion/oferta.py` (`crear_oferta`, `fin_de_vigencia`, `vigente_hasta_de`). Puros: sin reloj ni E/S.

| Campo | Origen |
| --- | --- |
| `id`, `codigo` | UUID nuevo; código de la secuencia al guardar |
| `cotizacion_id` | La cotización |
| `precio`, `moneda`, `periodicidad` | La prima total de HU-W03, en la moneda del producto |
| `coberturas[{codigo, nombre, sumaAsegurada}]` | Las coberturas incluidas; `sumaAsegurada = R(suma × porcentaje)` con el redondeo de las reglas (sin entrada, toda la suma). En C01 con Enfermedades graves: 180.000.000, 180.000.000 y 54.000.000 (30 %) |
| `exclusionesResumidas[{codigo, descripcion}]` | Las de las reglas (4 en `2026.10.1`) |
| `version`, `versionReglas` | Versión del documento de la oferta (1) y de las reglas con que se calculó |
| `creadaEn`, `vigenteHasta` | El instante del cálculo y el fin de la vigencia |
| `estado` | `VIGENTE` o `VENCIDA` al instante de la consulta |

**Regla de dominio `Oferta.asegurar_utilizable(ahora)`**: lanza `OfertaVencida(oferta_id, vigente_hasta)` si `ahora ≥ vigenteHasta`; la API de `cotizacion` la traduce a 409 `OFERTA_VENCIDA` con `detalles.accion = "NUEVA_COTIZACION"` (ver sección 8).

La API interna (`/v1/cotizaciones`, `contracts/openapi/cotizacion.yaml`) agrega el campo opcional `oferta` (`OfertaDto`) a `CotizacionRespuesta`; `bff-web` lo copia (`test_dto_de_cotizacion_iguales_a_cotizacion`). Los insumos de suscripción llenan `ofertaId` y `codigo` y toman `vigenteHasta` de la oferta.

## 3. Contrato público del socio (v1)

`api-socios` (`adapters/inbound/socio.py`, `application/ofertas.py`, `domain/ofertas.py`, `adapters/outbound/cotizacion_http.py`). Se quitó el 501 de las dos rutas y se conserva el permiso `COTIZAR` (`domain/operaciones.py`).

| Ruta | Respuestas |
| --- | --- |
| `POST /cotizaciones` (`Idempotency-Key` obligatorio) | **201** `OfertaV1`; 200 al repetir la clave con el mismo cuerpo; 400 `ERROR_HTTP` (bytes ilegibles, como `/admin/socios`); 401/403 del borde y del permiso; 404 `CLIENTE_NO_ENCONTRADO`; 410 `OFERTA_VENCIDA`; 422 (validación y rechazos de HU-W03); 503 `SERVICIO_NO_DISPONIBLE` |
| `GET /cotizaciones/{cotizacion_id}` (UUID) | **200** `OfertaV1`; 404 `OFERTA_NO_ENCONTRADA` (inexistente, **de otro socio** o anterior a HU-W04, sin distinguir); 410 `OFERTA_VENCIDA`; 422; 503 |

- **Solicitud (`SolicitudCotizacionV1`)**: `clienteId`, `productoId`, `datosAsegurado` (`fechaNacimiento`, `plazoCreditoMeses`, `sumaAsegurada`) y `coberturasOpcionales`. `additionalProperties: false`: no admite `perfilId` ni `socioId` (el socio es la credencial).
- **Respuesta (`OfertaV1`, lista blanca)**: `id`, `codigo`, `cotizacionId`, `productoId`, `precio`, `moneda`, `periodicidad`, `coberturas[{codigo, nombre, sumaAsegurada}]`, `exclusionesResumidas[{codigo, descripcion}]`, `vigenteHasta`, `estado`, `version`, `versionReglas`, `creadaEn`. Sin hash ni huella de las reglas, sin insumos (edad, nivel de riesgo), sin el desglose ni ids internos (perfil, actor, cliente).
- **Hacia `cotizacion`**: `X-Actor-Id` = id del socio, `X-Actor-Rol: SOCIO`, `Idempotency-Key` y la correlación (`X-Correlation-Id` y `traceparent`, cliente de `solventa_common`). Sin `socioId` ni `perfilId` en el cuerpo. Lector tolerante: solo se valida `productoId` y `oferta`. Timeout `cotizacion_timeout_ms` (1.500 ms, como `bff-web`).
- **Métricas**: `socio.cotizar` y `socio.consultar_oferta` (`contracts/operaciones.md`): `ok`, `rechazado` (4xx, incluido el 410) y `error`/`timeout` (503).
- **Logs**: solo identificadores (`socio_id`, `oferta_id`). Nunca la fecha de nacimiento ni la suma asegurada (`test_mide_las_operaciones_y_no_registra_pii` y los centinelas de módulo de `cotizacion`, que capturan también a `api-socios`).

## 4. Compatibilidad hacia atrás (AC2)

- **Contrato publicado congelado**: `contracts/openapi/publicados/api-socios.v1.yaml` (operaciones del socio disponibles: `/socio/me` y `/cotizaciones`; sin `/admin`, `/health` ni las que responden 501). `tests/contract/test_contrato_publicado.py` falla si se elimina o renombra un campo, cambia su tipo, un opcional de la solicitud pasa a obligatorio o aparece uno obligatorio, un campo obligatorio de la respuesta deja de serlo, desaparece una operación, un código o un parámetro, o un valor de un enum. Siete pruebas demuestran que el comparador detecta cada caso y que los cambios aditivos son compatibles. Un cambio incompatible es una v2.
- **Pacto del socio de referencia** (`contracts/pact/socio-referencia-api-socios.json`): un cliente mínimo (`SocioReferencia`) cotiza, consulta y presenta la oferta solo con los campos publicados (matchers de tipo y formato), recotiza ante 410 y recibe 404 por la oferta de otro socio. `api-socios` lo verifica con su app real (autenticación, permiso y DTO) y `cotizacion` simulado.
- **Pacto `api-socios` → `cotizacion`** (`contracts/pact/api-socios-cotizacion.json`): cotizar como socio, leer la oferta, 404 de la de otro socio y 422 `PRODUCTO_FUERA_DE_VIGENCIA`. `cotizacion` lo verifica con su app real y las reglas `2026.10.1`.
- El pacto `polizas` → `cotizacion` se regeneró: S01 y S08 ya traen `ofertaId` y `codigo` (matchers de formato); `cotizacion` lo sigue verificando.

## 5. Borde e infraestructura

- `infra/platform/edge.tf`: `/{proxy+}` del API de socios ya lleva `POST` y `GET` a `api-socios` con el autorizador de Cognito (`authorization_scopes` de socios), `api_key_required` y el plan de uso por API key. El stage es `v1`. **Cambio mínimo:** en el dominio propio con mTLS, `base_path = "v1"` (antes la raíz), para que el contrato v1 tenga la misma URL en execute-api y en el dominio y una v2 pueda convivir; `api_urls.socios_mtls` incluye `/v1` y `infra/platform/tests` lo verifica.
- `infra/envs/int/apps.tfvars`: `CATALOGO_PRODUCTOS_QA = "true"` para `catalogo` (producto `vida-hipotecario-qa-oferta-corta`, oferta de un minuto, en `semillas/qa/productos_qa.json`; sin la variable no se publica).
- `api-socios` → `cotizacion` ya estaba en el catálogo de infraestructura (`calls = ["cotizacion"]` define `COTIZACION_URL`) y en los diagramas (`APISocios --> PerfilCotizacion`).

## 6. Pruebas y resultados (ejecutados el 2026-10-22, en local)

| Nivel | Qué | Resultado |
| --- | --- | --- |
| N1 `cotizacion` | Mapeo prima → oferta, suma por cobertura con el redondeo, vigencia de las reglas o de la configuración, instante exacto (−1 µs vigente, exacto y +1 µs vencida), `asegurar_utilizable`, 409 de `OfertaVencida`, repositorio (transacción, código, `LEFT JOIN`), API interna (oferta vigente, vencida al leer, sin oferta, socio ajeno) (`tests/unit/cotizacion/test_oferta.py`, `test_cotizaciones_pg.py`) | `make test-cov SERVICE=cotizacion`: **579 passed**; líneas **98,99 %**, ramas **97,21 %** |
| N1 `api-socios` | Rutas, lista blanca del DTO, 410 en `GET` y al repetir la clave, 404 de otro socio, rechazos de W03 con su código, 503 sin topología (5xx, 403, códigos ajenos, cuerpo fuera de contrato, timeout, conexión), validación (perfilId, socioId, campos extra, clave obligatoria, id no UUID), métricas y PII (`tests/unit/test_ofertas.py`) | `make test-cov SERVICE=api-socios`: **177 passed**; líneas **99,28 %**, ramas **97,83 %** |
| N1 `catalogo` | Esquema y validación del bloque `oferta` (límites, duplicados, coberturas ajenas, porcentajes), hash sin cambios en `2026.09.1`, semilla `2026.10.1` y de QA, arranque con `CATALOGO_PRODUCTOS_QA` (`tests/unit/productos/test_condiciones_oferta.py`) | `make test-cov SERVICE=catalogo`: **231 passed**; líneas **99,45 %**, ramas **98,17 %** |
| N1 `bff-web` | La oferta llega al portal tal cual, también vencida | `make test-cov SERVICE=bff-web`: **257 passed**; líneas **98,27 %** |
| Contrato | OpenAPI regenerados (`api-socios`, `cotizacion`, `bff-web`, `catalogo`) y `reglas-prima.v1` (CT-W03-01); Schemathesis sobre `api-socios.yaml` (con los códigos de W04: 201, 422, 503, 200, 410 y 404) y `cotizacion.yaml`; contrato publicado; pactos socio de referencia → `api-socios`, `api-socios` → `cotizacion` y `polizas` → `cotizacion` | `make test-contract`: `api-socios` **38**, `polizas` **29**, `cotizacion` **25**, `catalogo` **18**, `bff-web` **22** passed |
| N2 `cotizacion` | PostgreSQL, `catalogo` y `clientes` efímeros, y el canal del socio de punta a punta (`api-socios` real con su base → `cotizacion` servida con uvicorn): oferta guardada con la cotización, códigos únicos y crecientes, inmutabilidad, migración idempotente, AC1 por el canal, socio ajeno 404, producto vencido 422, AC3 410 (`tests/module/test_oferta_w04.py`) + W03, W10–W13, W31 y centinelas | `make test-module SERVICE=cotizacion`: **94 passed** |
| N2 `api-socios` | Socios en PostgreSQL y Cognito/API Gateway (moto): la oferta exige `cotizar` y va a nombre del socio | `make test-module SERVICE=api-socios`: **7 passed** |
| N2 `catalogo`, `polizas` | Semillas (tres versiones, hash), productos de QA solo con la bandera e idempotentes; regresión de la suscripción | **23** y **7 passed** |
| N3 R1 | `tests/e2e/test_hu_w04_oferta.py` contra `int` | **No ejecutado**: la plataforma está destruida. Sin ambiente se omite con el motivo (2 skipped) |
| Regresión | `make test-unit` (todo el monorepo) | **2492 passed** |
| Calidad | `ruff check` y `ruff format --check` en `catalogo`, `cotizacion`, `api-socios`, `bff-web` y `polizas`; `mypy` | Limpios. `mypy` de `api-socios` informa 2 `unused-ignore` en `dependencias.py` que ya están en `main` |
| Seguridad | Bandit (`services/{api-socios,cotizacion,catalogo}/src`) | 0 altos; 18 medios, todos B608 (SQL con columnas constantes y valores parametrizados; `cotizaciones_pg` pasa de 2 a 3 por el `INSERT` de la oferta) |
| Infraestructura | `fmt`, `validate` y `terraform test` de `bootstrap`, `platform` y `apps` (con un `TF_DATA_DIR` limpio: el `.terraform` local guarda el backend S3 de un despliegue anterior) | `platform` **7 passed** (incluida la del base path `v1`), `apps` **8 passed**. Trivy no está instalado en esta máquina: no se ejecutó |
| Portal | `npm run test:ci` (Karma, axe y compuerta por feature) y `ng build` | **444 SUCCESS**; feature `cotizacion` líneas **99,53 %**, ramas **86,73 %**; build correcto |

## 7. Traspaso de HU-W13

Lo que pedía [docs/hu-w13-suscripcion.md](hu-w13-suscripcion.md#vigencia-de-la-oferta-para-hu-w04):

1. **Hecho.** `vigenteHasta` sale de la oferta guardada en `respuesta_de` y en `ConsultarInsumos` (`vigente_hasta_de`).
2. **Hecho.** El nombre y el significado de `vigenteHasta` no cambian (instante UTC, `ahora < vigenteHasta`).
3. **Hecho.** `ofertaId` y `codigo` se llenan en `InsumosSuscripcion`; `polizas` los guarda sin cambios de código.
4. **Hecho.** `polizas-cotizacion.json` se regeneró y `cotizacion` lo verifica; `test_openapi.py` de `cotizacion` y `bff-web` en verde.
5. **Parcial, a propósito.** `oferta_vigencia_horas` se **conserva** como respaldo para las reglas sin condiciones de oferta y las cotizaciones anteriores a la migración 004. No hay cambio para el dueño de `infra/`.

Pruebas de HU-W13 ajustadas al nuevo comportamiento (no a otro resultado): `tests/unit/suscripcion/test_insumos.py` (vigencia guardada; el parámetro solo aplica sin oferta) y `tests/module/test_suscripcion_w13.py` (7 días de las reglas en lugar de 96 h; con `oferta_vigencia_horas=0` la oferta ya no nace vencida).

## 8. Nota para el dueño de HU-W15 (aceptación)

- **La regla:** `Oferta.asegurar_utilizable(ahora)` (`services/cotizacion/src/cotizacion/domain/cotizacion/modelos.py`). Es pura y semiabierta: utilizable si `ahora < vigenteHasta`; lanza `OfertaVencida(oferta_id, vigente_hasta)` en el instante exacto o después. Úsala antes de registrar la aceptación, con el reloj del servicio que decide, nunca con una fecha que envíe el cliente.
- **El código:** si la aceptación pasa por una ruta de `cotizacion`, agrega `OfertaVencida` a `ERRORES_TRADUCIBLES` de esa ruta: `error_cotizacion` ya la traduce a **409 `OFERTA_VENCIDA`** con `detalles.accion = "NUEVA_COTIZACION"` (el mismo que la suscripción). Si la aceptación es del canal del socio (`api-socios`), responde **410**, como la consulta (`error_oferta` en `adapters/inbound/socio.py`).
- **Si la aceptación vive en `polizas`:** W13 ya guarda `vigenteHasta` en la instantánea de la decisión (DS2-10) y `polizas/domain/suscripcion/vigencia.py` aplica el mismo criterio semiabierto. Valida contra esa instantánea (es la misma `vigenteHasta` que la oferta guardada) o llama a `cotizacion`. No dupliques la vigencia con otro significado.
- **Identificadores:** acepta por `ofertaId` (UUID) y muestra `codigo`. Ambos llegan en los insumos de suscripción y en la decisión de `polizas`.
- **Inmutabilidad:** la fila de `ofertas` no cambia (trigger). Registra la aceptación en tu propia tabla con el `ofertaId`; el estado ACEPTADA no se escribe en `ofertas`.
- **Pruebas que puedes reutilizar:** `test_vigencia_en_el_instante_exacto` y `test_asegurar_utilizable` (`services/cotizacion/tests/unit/cotizacion/test_oferta.py`).

## 9. AC → evidencia

| Criterio | Evidencia |
| --- | --- |
| **AC1** Con un cálculo de prima exitoso, la oferta devuelve precio, moneda, coberturas, vigencia e identificador | N1 `test_la_oferta_se_crea_con_la_prima_y_las_condiciones_de_las_reglas`, `test_la_respuesta_trae_la_oferta_vigente` (cotizacion), `test_ac1_cotizar_devuelve_la_oferta_publica` (api-socios); N2 `test_ac1_la_oferta_se_guarda_con_la_cotizacion`, `test_ac1_el_socio_recibe_la_oferta_publica`; N3 `test_hu_w04_r1_oferta_con_token_y_api_key`; portal C04 (`cotizacion-calculada.component.spec.ts`) |
| **AC2** Un consumidor compatible con la versión publicada interpreta la oferta sin conocer el modelo interno | Contrato congelado `publicados/api-socios.v1.yaml` y `test_el_contrato_actual_es_compatible_con_el_v1_publicado` (+7 pruebas del comparador); pacto `socio-referencia-api-socios.json` verificado por `api-socios`; lista blanca (`test_hu_w04_la_oferta_publica_no_expone_campos_internos`, `test_el_dto_publico_es_la_lista_blanca`); N3 sin campos internos |
| **AC3** Si la vigencia terminó y el socio intenta usar la oferta, Solventa informa que debe cotizar de nuevo | N1 `test_vigencia_en_el_instante_exacto`, `test_asegurar_utilizable`, `test_ac3_oferta_vencida_es_410_con_la_accion`, `test_ac3_repetir_la_clave_de_una_oferta_vencida_es_410`; pacto (`test_ac3_una_oferta_vencida_pide_cotizar_de_nuevo`); N2 `test_ac3_una_oferta_vencida_es_410_para_el_socio`; N3 `test_hu_w04_r1_oferta_vencida_es_410`; portal C08 |
| Regla: identificador, precio, moneda, coberturas, exclusiones resumidas, vigencia y versión | `OfertaV1` y `OfertaDto`; `test_ac1_*` |
| Regla: el socio recibe solo los datos necesarios | Lista blanca (sección 3) y pruebas de AC2 |
| Regla: una oferta vencida no puede aceptarse | `asegurar_utilizable` (sección 8); 410 en el canal; el portal no ofrece continuar con una oferta vencida |
| Seguridad: un socio no lee la oferta de otro | `test_seguridad_la_oferta_de_otro_socio_es_404` (N1), `test_seguridad_un_socio_no_lee_la_oferta_de_otro` (N2), `test_el_socio_no_lee_la_oferta_de_otro` (cotizacion), pactos, N3 |
| Seguridad: sin PII en logs | `test_mide_las_operaciones_y_no_registra_pii`; centinelas de módulo de `cotizacion` (incluyen los logs de `api-socios` en la cadena de punta a punta) |

## 10. Pendiente

- **Ejecutar en AWS (`int`)**: con la plataforma arriba, `make infra-aplicaciones ENV=int` (variable de `catalogo`), `make infra-microservicios SERVICES=catalogo,cotizacion,api-socios,bff-web ENV=int` y `uv run pytest tests/e2e -k hu_w04` (N3 R1, ~1,5 min por la espera de la oferta de un minuto). Medir el p95 de `socio.cotizar` en el tablero.
- **Trivy**: correr `make infra-validar` donde esté instalado (en esta máquina no está).
- **`socio-a` en el N3**: los socios de Terraform no están en la base de `api-socios`, así que no llegan a las rutas de dominio. Si se quiere usar `socio-a`, hace falta una historia que los registre (por ejemplo, una semilla de `api-socios` desde las salidas de Terraform). Hoy el N3 da de alta sus propios socios, como HU-W01.
- **Portal**: revisión visual de C04 y C08 contra Figma con la app corriendo (se probaron con Karma y axe). El título del mockup "Oferta para *nombre del cliente*" necesita el nombre desde `clientes` en `bff-web` (como `clienteNombre` en las vistas de suscripción); hoy el título es "Oferta" con el código. "Recalcular ahora" abre "Nueva cotización": la fecha de nacimiento no se guarda (minimización), así que no se puede recalcular sin volver a pedirla. "Comparar opciones" (C05) y "Ver explicación" (C06) no se muestran.
- **Frontend**: después de fusionar el backend, `scripts/sync-contracts.sh <sha>` en el repo del frontend para fijar `contracts/openapi/VERSION` (el `bff-web.yaml` ya es el de esta rama).
- **Revisión por otra persona** de las condiciones de la oferta en `2026.10.1` (vigencia de 7 días, exclusiones y porcentajes de suma) con negocio y actuaría.
