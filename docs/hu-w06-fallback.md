# HU-W06 · Respuesta degradada permitida

> Como socio de distribución quisiera que Solventa use una respuesta degradada permitida cuando una fuente falle para mantener disponible el recorrido de cotización (issue #48).

## 1. Alcance y decisiones

Decisiones acordadas antes de implementar:

| Tema | Decisión |
| --- | --- |
| Dónde se decide | **En `cotizacion`, al cotizar, con el linaje del perfil.** El recorrido de cotización no consulta las fuentes (HU-W05, `modelo_componentes.puml`): las consulta el perfilamiento y la cotización lee el perfil. `decidir_fallback` toma el estado de cada fuente del linaje (`TIEMPO_AGOTADO`, `NO_DISPONIBLE`, `RESPUESTA_INVALIDA`) y las reglas de `catalogo`, que se leen por la llamada (3) que ya existe. No hay flechas nuevas en los diagramas ni cambios en el perfilamiento (W10–W12) |
| Estado recuperable (AC2) | **503 `DEPENDENCIA_NO_DISPONIBLE`**, el código recuperable que ya usan HU-W04 y HU-W05 y que `api-socios` ya traduce a 503 `SERVICIO_NO_DISPONIBLE`. Con `detalles.dependencia = perfil.fuentes`, más `decisionId`, `faltantes`, `versionReglasFallback` y `accionSugerida = RECALCULAR_PERFIL` |
| Obligatoriedad | **La define el modelo de riesgo.** Una fuente es obligatoria si aporta un campo `obligatorio` del modelo. La regla declara `obligatoria`; al sembrar, `catalogo` exige que coincida con el modelo y rechaza una regla `DEGRADAR` sobre una fuente obligatoria. Al cotizar, también es obligatoria la fuente que el motor listó en `datosRequeridos` del perfil. Basta que una de las dos fuentes de verdad la marque así para que no se degrade |
| Frontend | **C07 adaptado a lo que entrega el backend** (sección 7) |

Ajustes al diseño propuesto, con su motivo:

- **`accion` solo admite `DEGRADAR` o `PENDIENTE`; no hay `RECHAZAR`.** Una caída del proveedor es transitoria: un rechazo definitivo por esa causa contradice "estado pendiente o rechazo controlado" cuando sí existe un estado recuperable. Agregar `RECHAZAR` después es un cambio aditivo al esquema.
- **`PoliticaDependencias` (W05) no cambia.** Las dependencias del recorrido (`catalogo.producto`, `catalogo.reglas_prima`, `clientes.identidad` y el perfil) no admiten sustituto: sin producto, reglas o identidad no hay un precio que no sea inventado. Las reglas de fallback son una dependencia más con falla cerrada: si `catalogo` no las entrega, la respuesta es 503 con `catalogo.reglas_fallback` y no se toma ninguna decisión.
- **Una fuente sin regla vigente no es degradable**, y tampoco lo es ninguna fuente cuando no hay reglas vigentes: reglas vencidas, futuras o inexistentes.
- **La regla `FB-004` (`DA_REGISTROS_PUBLICOS`) es `DEGRADAR`.** El N3 de HU-W05 y el caso S08 de HU-W13 cotizan con C06, que tiene esa fuente lenta. Degradarla es seguro: sin `coincidencias_listas_restrictivas`, la regla SUS-015 de suscripción queda `NO_EVALUABLE` y la solicitud va a `REVISION_REQUERIDA` (`polizas/domain/suscripcion/motor.py`: el motor nunca aprueba sin el dato).
- **Socios:** un socio cotiza sin perfil (`socio_de`, HU-W03/W04), así que en el canal del socio `degradada` y `faltantes` llegan hoy siempre en `false` y vacíos. Se agregaron al contrato v1 como campos opcionales, compatibles hacia atrás. El recorrido degradado real es el del asesor.
- **Un perfil `INFORMACION_INSUFICIENTE` por consentimiento** (`NO_AUTORIZADA` o `NO_CONSULTADA`) no es una falla de la fuente y sigue cotizando como antes, con `SIN_PERFIL`. HU-W06 solo actúa sobre fuentes que fallaron.
- **Diagramas:** sin cambios. El outbox de `cotizacion` ya existía desde HU-W11 (`PerfilRiesgoCalculado`).

## 2. Reglas de fallback versionadas (`catalogo`)

- **Esquema:** [`contracts/modelos/reglas-fallback.v1.schema.json`](../contracts/modelos/reglas-fallback.v1.schema.json), generado desde `catalogo/adapters/inbound/http/esquema_fallback.py`.
- **Regla:** `{id FB-NNN, fuente, obligatoria, accion DEGRADAR|PENDIENTE, ajuste, mensajeCodigo}`.
  - `ajuste` es un factor en texto decimal entre 0,50 y 2,00, o `null`.
  - `mensajeCodigo` es una clave de texto, nunca texto libre.
- **Versionado igual que las reglas de suscripción (HU-W13):**
  - Tabla `reglas_fallback` (migración `004`), inmutable, con `hash` canónico y semilla por migración.
  - `vigente_desde` y, a diferencia de las reglas de prima, **`vigente_hasta`**, porque un permiso para degradar puede ser temporal.
  - La vigente es la de mayor `vigente_desde ≤ fecha`. Si esa versión ya venció no hay reglas vigentes: no se vuelve a la anterior.
- **API:** `GET /v1/productos/{productoId}/reglas-fallback/vigente?fecha=` y `.../reglas-fallback/{version}` (404 `REGLAS_NO_VIGENTES` o `PRODUCTO_NO_ENCONTRADO`). Operación `reglas_fallback.consultar`.
- **Validación al sembrar:** una semilla inválida impide arrancar.
  - Fuente fuera del modelo.
  - Obligatoriedad distinta del modelo.
  - Obligatoria con `DEGRADAR`.
  - Ajuste sin `DEGRADAR` o fuera de rango.
  - Id o fuente duplicados.
  - Vigencia inválida.

Semilla `vida-hipotecario` `2026.10.1`, vigente desde el 2026-10-01, sin vencimiento:

| Id | Fuente | Obligatoria (modelo 2026.10.1) | Acción | Ajuste |
| --- | --- | --- | --- | --- |
| FB-001 | `FA_PRODUCTOS_VIGENTES` | sí (`cuota_mensual_obligaciones`) | `PENDIENTE` | — |
| FB-002 | `FA_HISTORIAL_PAGOS_12M` | sí (`moras_12m`) | `PENDIENTE` | — |
| FB-003 | `FA_INGRESOS_AGREGADOS` | no | `DEGRADAR` | — |
| FB-004 | `DA_REGISTROS_PUBLICOS` | no | `DEGRADAR` | — |

## 3. Matriz de decisión (`decidir_fallback`)

La función es pura (`cotizacion/domain/cotizacion/fallback.py`): sin reloj, sin E/S y sin `float`. Las entradas son las fuentes fallidas del linaje, las reglas y el instante de la cotización.

| # | Fuentes del perfil | Reglas | Decisión | Resultado |
| --- | --- | --- | --- | --- |
| 1 | Ninguna falló (`CONSULTADA`, `NO_AUTORIZADA` o `NO_CONSULTADA`) | No se piden | — | Oferta definitiva, `degradada: false` |
| 2 | Opcional fallida (tiempo agotado, caída o malformada) | Vigentes, regla `DEGRADAR` | **DEGRADADA** | 201: oferta provisional, `degradada: true`, `faltantes[{fuente, dato, motivo, regla{id, version}}]`. El dato queda ausente; el nivel es el que calculó el motor con las fuentes que respondieron |
| 3 | Igual que 2, con `ajuste` | Vigentes | **DEGRADADA** | Igual que 2, más el factor `FALLBACK` (valor = fuente) en el desglose, que multiplica la prima ajustada y queda en la huella del cálculo |
| 4 | Opcional fallida | Vigentes, regla `PENDIENTE` | **PENDIENTE** | 503, sin oferta |
| 5 | Opcional fallida | Vigentes, sin regla para esa fuente | **PENDIENTE** | 503; `regla: null` |
| 6 | Obligatoria fallida (en `datosRequeridos` o regla `obligatoria`) | Cualquiera | **PENDIENTE** | 503. Ni una regla mal declarada la degrada |
| 7 | Cualquiera fallida | Sin reglas vigentes: inexistentes, futuras (`vigenteDesde > ahora`) o vencidas (`ahora ≥ vigenteHasta`) | **PENDIENTE** | 503; `regla: null` y `versionReglasFallback: null` |
| 8 | Varias fallidas | — | PENDIENTE si alguna lo es; si no, DEGRADADA | Se informan todas, ordenadas por fuente. Con PENDIENTE no hay ajustes |
| 9 | `catalogo` no entrega las reglas (caída o timeout) | — | Ninguna | 503 `DEPENDENCIA_NO_DISPONIBLE` con `catalogo.reglas_fallback` (falla cerrada, como W05); no se registra decisión |

**Respuesta malformada:**
- En `adaptador-datos`, `proyectar` es todo o nada: un campo inválido invalida la respuesta completa (`RESPUESTA_INVALIDA`, 502, resultado `error`).
- En `cotizacion`, la fuente queda `RESPUESTA_INVALIDA` y sin campos en el linaje, y ningún campo entra a los insumos del perfil.
- A partir de ahí se decide como cualquier otra falla: filas 2 a 8.

**Nunca se inventa un valor.**
- La decisión solo nombra la fuente, el dato (nombre del campo) y el motivo.
- La respuesta no tiene ningún campo para el valor faltante.
- El precio usa el nivel calculado con las fuentes que respondieron y, si la regla lo dice, un ajuste explícito en el desglose.

## 4. Contrato

| Dónde | Cambio |
| --- | --- |
| `cotizacion` `POST/GET /v1/cotizaciones` | Aditivos: `degradada` (por omisión `false`), `faltantes[{fuente, dato, motivo, regla{id, version} \| null}]` y `versionReglasFallback`. Sin el hash de las reglas (minimización) |
| `cotizacion` `GET /v1/cotizaciones/{id}/decision` (nuevo, interno) | `{cotizacionId, decision NOMINAL\|DEGRADADA, decisionId, perfilId, faltantes, versionReglasFallback, mensajeCodigo, decididoEn}`. Para el asesor o el socio dueño (el de otro socio responde 404) |
| `cotizacion` 503 | `detalles` de la cotización pendiente (sección 1). Ver [`codigos-error.md`](../contracts/codigos-error.md) |
| `api-socios` `OfertaV1` | Opcionales: `degradada` (por omisión `false`) y `faltantes[{fuente, dato}]`. Sin regla, sin motivo y sin valor. La prueba del contrato publicado de HU-W04 (`test_contrato_publicado.py`) sigue en verde |
| `bff-web` | Copia de los DTO de `cotizacion` (`FaltanteDto`, `ReglaAplicadaDto` y `MotivoFalla`, verificados por `test_dto_de_cotizacion_iguales_a_cotizacion`). El 503 pendiente pasa al portal con sus `detalles` |
| `catalogo` | `reglas-fallback` (sección 2) |
| OpenAPI | `cotizacion.yaml`, `catalogo.yaml`, `api-socios.yaml` y `bff-web.yaml` regenerados |

## 5. Trazabilidad y auditoría (AC3)

- **`decisiones_fallback`**: migración `005` de `cotizacion`, inmutable. Guarda:
  - La fuente, el dato y el motivo de cada faltante.
  - Si la fuente es obligatoria, y la regla (id, versión y acción).
  - El tipo de decisión, el mensaje, la versión y la huella de las reglas, y el ajuste.
  - El actor, la clave de idempotencia, la huella de la solicitud, la correlación y el instante.
- **DEGRADADA**: se guarda en la misma transacción que la cotización y la oferta, unida a la cotización (`cotizacion_id`).
- **PENDIENTE**: va sola, una vez por actor y clave. Repetir la clave devuelve la misma decisión sin un evento nuevo; la misma clave con otra solicitud es `IDEMPOTENCIA_CONFLICTO`. Después de recalcular el perfil se cotiza con una clave nueva.
- **Evento [`CotizacionDegradada.v1`](../contracts/events/CotizacionDegradada.v1.schema.json)** en el outbox, en la misma transacción. `aggregateId` es la cotización (DEGRADADA) o la decisión (PENDIENTE).
- **Minimización:** solo identificadores, nombres de campos y códigos. Ni `clienteId`, ni montos, ni datos declarados, ni valores. Lo verifican N1, N2 y los centinelas de la suite de módulo.
- **Consulta:** `GET /v1/cotizaciones/{id}/decision` y, dentro de la cotización, `faltantes` con la regla.
- **Logs:** el cierre `cotizacion calculada` agrega `fallback`; la cotización pendiente emite un WARNING `cotizacion pendiente`. Ver [`operaciones.md`](../contracts/operaciones.md).

**Cadena hasta el bucket con Object Lock.** La infraestructura existe: SNS de negocio → cola `auditoria` (recibe todo, sin filtro) → worker `auditoria` → S3 con Object Lock (`infra/platform`). En el código de `main` faltan dos piezas, y ninguna es de esta historia:
- **El relay del outbox:** `solventa_common/outbox.py` está vacío y ningún servicio publica su outbox.
- **El worker de `auditoria`:** el servicio solo expone `/health`.

Por eso el evento de HU-W06 queda escrito, válido contra su esquema y con `published_at` nulo (N2). Lo mismo ocurre hoy con `PerfilRiesgoCalculado` y `DecisionSuscripcionRegistrada`. Ver la sección 10.

## 6. Telemetría y alarmas

- **`operacion("cotizacion")`** suma dos resultados: `degradado` (oferta provisional) y `pendiente` (503 sin oferta). Ninguno cuenta en `OperationErrors`: la falla ya la contó el proveedor, y no se alarma dos veces.
- **Dependencia nueva:** `cotizacion.dependencia.catalogo.reglas_fallback`, que solo se mide si una fuente faltó. Paso nuevo `REGLAS_FALLBACK` en los pasos de la respuesta.
- **Alarma de degradación por proveedor (HU-S02), sin cambios en `infra/apps/alarms.tf`.** `consulta.<FUENTE>` de `adaptador-datos` queda:
  - Caída: `error`.
  - Malformada: `error`.
  - Lenta: `timeout`.

  Los tres cuentan en `OperationErrors / SampleCount(OperationDuration)` (`test_metrica_y_log_con_resultado`). La alarma ya mide las cuatro fuentes (`provider_operations`). `terraform test`: **8 passed**.

## 7. Frontend (portal del asesor)

C07 adaptado, en `cotizacion-calculada`:
- Título "Oferta calculada con información parcial".
- Aviso con las fuentes que no respondieron.
- Etiqueta "Oferta provisional".
- Tabla "Datos que faltaron": fuente, dato, motivo y regla aplicada con su versión.
- Nota de las reglas de fallback.
- "Continuar con esta oferta" lleva a la suscripción de HU-W13.
- "Recalcular el perfil después" lleva al perfil, donde está el recálculo de HU-W12.
- Un ajuste de la regla aparece en el desglose como "Ajuste por dato faltante".

Cotización pendiente, en la página del perfil:
- La alerta del 503 explica que no hay oferta definitiva y lista qué faltó, sin valores.
- El botón "Recalcular el perfil" ya está en esa página (perfil degradado o insuficiente recalculable).

Diferencias con el Figma (manda el backend):
- **"Valor por defecto" (paso "Finanzas Abiertas")** no se muestra: el dato no se rellena.
- **"Tarifa base sin ajuste por comportamiento"** no aplica: el precio usa el nivel calculado con las fuentes que respondieron.
- **El panel "Qué ocurrió con la dependencia"** (presupuesto, reintentos, estado del circuito y próxima comprobación) no se muestra. La cotización no conoce esos datos, que viven en el perfilamiento y en la telemetría (W05/W27). El Figma muestra además 2 reintentos y 120 ms; la regla común es 1 reintento, y en `int` son 600 ms.
- **"Esperar y recalcular después"** se convirtió en el enlace al perfil para recalcularlo.

## 8. Pruebas y resultados (ejecutados el 2026-10-27, en local)

| Nivel | Qué cubre | Resultado |
| --- | --- | --- |
| N1 `cotizacion` (`test_hu_w06_fallback.py`, `test_hu_w06_cotizaciones_pg.py`) | Tabla completa: opcional con y sin regla, con regla `PENDIENTE`, obligatoria (perfil y regla), reglas inexistentes, vencidas, futuras y en el límite, varias fuentes, malformada y ajuste. Nombres sin valor. Fuentes fallidas desde el linaje (C05, C06, C07 y C13, sin incluir `NO_AUTORIZADA`). Caso de uso: degradada, ajuste en el desglose, pendiente registrada e idempotente, `catalogo` caído o lento al pedir las reglas, nominal sin pedirlas, repetición de la clave. Telemetría `degradado` y `pendiente` y centinelas. API: campos, 503 con detalles y `/decision` por actor. `ProductosCatalogo.reglas_fallback`: 404, fuera de contrato y caché en memoria solo mientras rigen. Repositorio: decisión y evento en la misma transacción, idempotencia de la pendiente | `make test-cov SERVICE=cotizacion`: **656 passed**; líneas **98,85 %**, ramas **96,65 %**; `fallback.py` al 100 % |
| N1 `catalogo` (`tests/unit/fallback`) | Validación contra el modelo, API (vigencia, vencimiento y versión), siembra, repositorio y caso de uso | `make test-cov SERVICE=catalogo`: **265 passed**; líneas 99,22 %, ramas 97,29 % |
| N1 `adaptador-datos` | Un campo inválido invalida toda la respuesta (no entra ningún campo parcial) | **92 passed**; 100 % |
| N1 `api-socios`, `bff-web` | Oferta degradada con solo `{fuente, dato}`; lector tolerante; el 503 pendiente pasa al portal | **184** y **297 passed** |
| N2 `cotizacion` (`tests/module/test_hu_w06_fallback.py`, Postgres 16, WireMock 3.9.2, `catalogo` y `adaptador-datos` reales) | C07 caída → degradada con la decisión y el evento. Opcional malformada (mapeo sobrescrito solo en la prueba) → degradada, sin campos en el linaje ni en los insumos. C06 lenta → degradada (el recorrido de W05 sigue con oferta). Dos opcionales caídas → degradada con las dos. C05 obligatoria malformada y C13 obligatoria lenta → 503 pendiente, sin cotización, con la decisión y el evento. Opcional + obligatoria → pendiente. C01 → nominal. Cada evento se valida contra su esquema y no contiene datos sensibles | **8 passed** (salida en [n2-hu-w06.txt](evidencias/hu-w06/n2-hu-w06.txt); evento real en [evento-cotizacion-degradada.json](evidencias/hu-w06/evento-cotizacion-degradada.json)) |
| N2 `catalogo` (`test_reglas_fallback.py`) | Semilla idempotente con hash, versión única, inmutabilidad, restricciones, vencimiento sobre la base real, migración adelante y atrás | **6 passed** (29 del servicio) |
| Contrato | Esquema publicado igual al generado (`catalogo`) y a la copia del consumidor (CT-W06-01), eventos, OpenAPI, contrato v1 publicado del socio, DTO del BFF, Schemathesis | `make test-contract`: `cotizacion` **27**, `catalogo` **27**, `api-socios` **38**, `bff-web` **26**, `adaptador-datos` **4** |
| N3 R1 (`tests/e2e/test_hu_w06_r1_fallback.py`) | Nominal C01, degradado C07 (consulta la cotización con su regla) y pendiente C13, por el borde real del portal | **No ejecutado**: la plataforma está destruida. Sin ambiente se omite con el motivo (3 skipped) |
| Regresión | `make test-unit` (todo el monorepo); `make test-module` de `cotizacion` (W03, W04, W05 por N2, W10–W13, W31 y centinelas), `catalogo` y `adaptador-datos` | **2840 passed**; módulo **102**, **29** y **19 passed**. Lint y `mypy` limpios, salvo los 2 `unused-ignore` de `api-socios/dependencias.py`, que ya están en `main` |
| Infraestructura | `terraform fmt`, `validate` y `test` de `infra/apps` (sin cambios) | **8 passed** |
| Frontend | `npm run test:ci` (incluye C07, la pendiente, en-US y axe) y `ng build` | **501 SUCCESS**; sentencias 95,36 %, ramas 86,06 % |

Resumen de las corridas en [regresion.txt](evidencias/hu-w06/regresion.txt). Testcontainers necesita `TESTCONTAINERS_RYUK_DISABLED=true` en esta máquina.

## 9. AC → evidencia

| Criterio | Evidencia |
| --- | --- |
| **AC1** Si falla una fuente opcional y hay una regla de fallback vigente, Solventa devuelve el resultado degradado, identificado como tal | N1 `test_tabla_de_fallback[opcional con regla DEGRADAR…]`, `test_ac1_opcional_caida_con_regla_da_oferta_degradada`, `test_ac1_el_ajuste_de_la_regla_queda_en_el_desglose`, `test_api_oferta_degradada_identificada_y_su_decision`. N2 `test_n2_c07_…`, `test_n2_opcional_malformada_…`, `test_n2_c06_…`, `test_n2_las_dos_opcionales_…`. N3 `test_hu_w06_r1_ac1_ac3_…` (pendiente de ambiente). Portal: C07 |
| **AC2** Si falla una fuente obligatoria y no hay fallback permitido, no hay oferta definitiva y se comunica un estado recuperable | N1 `test_tabla_de_fallback[obligatoria…, sin regla…, vencidas…, futuras…]`, `test_ac2_sin_fallback_permitido_…` (4 casos), `test_ac2_repetir_la_clave_…`, `test_api_pendiente_es_503_recuperable_…`, `test_si_catalogo_no_entrega_las_reglas_falla_cerrado`. N2 `test_n2_sin_fallback_permitido_es_pendiente_sin_oferta` (C05 y C13) y `test_n2_una_opcional_y_una_obligatoria_…`. N3 `test_hu_w06_r1_ac2_…` (pendiente de ambiente). Portal: alerta pendiente |
| **AC3** De un resultado degradado se conoce la fuente ausente y la regla aplicada | `GET /v1/cotizaciones/{id}/decision` (N1 y N2); `faltantes[].regla{id, version}` en la cotización (N1, N2 y N3); fila en `decisiones_fallback` y evento `CotizacionDegradada` válido (N2, [evento real](evidencias/hu-w06/evento-cotizacion-degradada.json)) |
| Regla: solo se degrada cuando una regla versionada lo permite | Matriz, filas 4, 5 y 7; validación de `catalogo` (`OBLIGATORIA_NO_DEGRADABLE`, `OBLIGATORIEDAD_DISTINTA_DEL_MODELO`); reglas versionadas e inmutables con vencimiento (N2 `catalogo`) |
| Regla: la respuesta indica qué dato faltó y no inventa valores | `faltantes[{fuente, dato, motivo}]`; `test_cada_dato_faltante_se_nombra_sin_valor`; centinelas (6.500.000 nunca aparece); malformado todo o nada (`adaptador-datos`, N2 linaje e insumos) |
| Regla: una dependencia obligatoria fallida produce un estado pendiente | 503 `DEPENDENCIA_NO_DISPONIBLE` con `accionSugerida: RECALCULAR_PERFIL`, decisión PENDIENTE registrada y su evento |
| Seguridad: minimización (respuesta, logs y evento sin datos personales) | N1 (logs y spans), N2 (payload sin `clienteId` ni valores), `test_zz_centinelas` de la suite de módulo, lista blanca del socio (`DatoFaltanteV1 = {fuente, dato}`) |
| Seguridad: el registro de auditoría queda en el bucket con Object Lock | **Pendiente** (sección 10): el evento queda en el outbox; faltan el relay y el worker `auditoria` |

## 10. Pendiente

- **Ejecutar en AWS (`int`):**
  1. `make infra-microservicios SERVICES=catalogo,cotizacion,api-socios,bff-web ENV=int`, para las migraciones `004` de `catalogo` y `005` de `cotizacion`, que se aplican al arrancar.
  2. `uv run pytest tests/e2e -k "hu_w06 or hu_w05"`. El N3 de W05 sigue esperando oferta con C06, que ahora sale degradada (FB-004).
  3. Durante la corrida, verificar en CloudWatch que `proveedor-open-finance-degradado` pase a ALARM cuando C07 se repite por encima del 20 % y vuelva a OK, y que el tópico de alertas notifique las dos transiciones (`docs/hu-w28-alertas.md`).
- **Registro auditable en S3 con Object Lock:** el evento se escribe en el outbox, pero en `main` no existen el relay del outbox (`solventa_common/outbox.py` está vacío) ni el worker de `auditoria`. Afecta a todos los eventos, no solo a este. Necesita su propia historia (HU-M08 o la que corresponda en la infraestructura de auditoría).
- **Canal del socio:** cuando un socio pueda cotizar con perfil, `degradada` y `faltantes` ya están en el contrato v1.
