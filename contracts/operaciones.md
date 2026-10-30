# Operaciones medidas

Nombres de `Operation` en `OperationDuration` y `OperationErrors` (`solventa_common.telemetry.operacion`). El colector exporta `^Operation.*` con la dimensión `Operation`; no se crean métricas nuevas.

| Operación (`Operation`) | Servicio | Dónde | Resultados |
| --- | --- | --- | --- |
| `perfilamiento` | `cotizacion` | Caso de uso `crear_perfil` | `ok` (CALCULADO), `degradado` (DEGRADADO), `insuficiente` (INFORMACION_INSUFICIENTE), `rechazado` (4xx), `error` / `timeout` (5xx) |
| `perfilamiento.recalculo` | `cotizacion` | Caso de uso `recalcular_perfil` | Igual que `perfilamiento` |
| `perfilamiento.dependencia.clientes.identidad` | `cotizacion` | Llamada a la identidad del cliente | `ok`, `timeout`, `error` |
| `perfilamiento.dependencia.clientes.consentimiento` | `cotizacion` | Verificación del consentimiento | `ok`, `timeout`, `error` |
| `perfilamiento.comprobacion_final` | `cotizacion` | Comprobación final del consentimiento tras consultar las fuentes (HU-W31); un `timeout` o `error` responde `DEPENDENCIA_NO_DISPONIBLE` (`clientes.consentimiento`) | `ok`, `timeout`, `error` |
| `perfilamiento.dependencia.catalogo.modelo` | `cotizacion` | Consulta del modelo de riesgo | `ok`, `timeout`, `error` |
| `perfilamiento.dependencia.fuente.<FUENTE>` | `cotizacion` | Rama de cada fuente | `ok`, `timeout`, `error` |
| `consulta.<FUENTE>` | `adaptador-datos` | Caso de uso `consultar_fuente` | `ok`, `timeout`, `error`, `rechazado` |
| `verificacion.kyc` | `adaptador-identidad` | Caso de uso `VerificarIdentidad` (llamada al proveedor KYC, HU-M06) | `ok`, `timeout`, `error` |
| `identidad.verificar` | `clientes` | Caso de uso `VerificarIdentidad` (llamada a `adaptador-identidad`, HU-M06) | `ok`, `timeout`, `error`, `rechazado` (captura inválida o rechazo del proveedor) |
| `sesion.consultar_identidad` | `bff-movil` | Caso de uso `ConsultarSesion` (llamada a `clientes`) | `ok`, `timeout`, `error`, `rechazado` (cliente no encontrado) |
| `verificacion.enviar` | `bff-movil` | Caso de uso `VerificarIdentidad` (llamada a `clientes`, HU-M06) | `ok`, `timeout`, `error`, `rechazado` (captura inválida o rechazo) |
| `registro.verificar` | `bff-movil` | Caso de uso `VerificarRegistro` (verificación previa a la cuenta, HU-M06) | `ok`, `timeout`, `error`, `rechazado` (captura inválida, rechazo o conflicto del registro) |
| `registro.vincular` | `bff-movil`, `clientes` | Caso de uso `VincularRegistro` (primer ingreso, HU-M06) | `ok`, `timeout`, `error` (solo `bff-movil`), `rechazado` (token inválido o registro ya vinculado) |
| `consentimientos.otorgar` | `bff-movil` | Caso de uso `OtorgarConsentimiento` (llamada a `clientes`, HU-M08) | `ok`, `timeout`, `error`, `rechazado` (identidad no verificada, cliente no encontrado, ya vigente o solicitud rechazada) |
| `consentimientos.listar`, `consentimientos.detalle` | `bff-movil` | Caso de uso `ConsultarConsentimientos` (HU-M08) | `ok`, `timeout`, `error`, `rechazado` (solo `detalle`: no encontrado o ajeno) |
| `consentimientos.revocar` | `bff-movil` | Caso de uso `RevocarConsentimiento` (llamada a `clientes`, HU-M09) | `ok`, `timeout`, `error`, `rechazado` (no encontrado o ajeno). Revocar uno ya revocado o vencido es `ok` |
| `consentimientos.historial` | `bff-movil` | Caso de uso `ConsultarConsentimientos` (HU-M09) | `ok`, `timeout`, `error`, `rechazado` (no encontrado o ajeno) |
| `consentimientos.texto_vigente` | `bff-movil` | Caso de uso `ConsultarTextoVigente` (HU-M08) | `ok`, `timeout`, `error`, `rechazado` (propósito o idioma no soportados) |
| `modelo_riesgo.publicar`, `modelo_riesgo.consultar` | `catalogo` | Casos de uso del modelo | — |
| `producto.consultar`, `reglas_prima.consultar` | `catalogo` | Consulta del producto y de sus reglas de prima (HU-W03) | `ok`, `rechazado` (inexistente o sin reglas vigentes), `error` |
| `cotizacion.dependencia.catalogo.producto`, `cotizacion.dependencia.catalogo.reglas_prima`, `cotizacion.dependencia.clientes.identidad`, `cotizacion.dependencia.perfil`, `cotizacion.dependencia.catalogo.reglas_fallback` (HU-W06: solo si una fuente del perfil faltó) | `cotizacion` | Cada dependencia del cálculo de la prima (HU-W03); el "no existe" de una dependencia que respondió es `ok`. Desde HU-W05, `timeout` cuando agota su timeout efectivo dentro del presupuesto del recorrido (también el 504 de `catalogo` o `clientes`); el span lleva `solventa.presupuesto_ms`, `solventa.restante_ms` y `solventa.timeout_ms` | `ok`, `timeout`, `error` |
| `cotizacion.consultar` | `cotizacion` | Lectura de una cotización (HU-W03) | `ok`, `rechazado` (inexistente o de otro socio) |
| `cotizacion.listar` | `cotizacion` | Listado de cotizaciones con oferta ("Cotizaciones abiertas" del portal) | `ok`, `rechazado` (cursor inválido) |
| `cotizacion.condiciones` | `cotizacion` | Condiciones del producto para el formulario del portal (HU-W03) | `ok`, `rechazado` (tabla de decisión), `error` |
| `cache.leer`, `cache.escribir`, `cache.borrar` | `catalogo`, `cotizacion` | Cache-Aside sobre Valkey (`solventa_common.cache`, HU-W03). Una falla nunca se propaga: el servicio va a su fuente de verdad | `ok`, `vacio` (lectura sin la clave), `timeout`, `error` |
| `habilitar_socio`, `listar_socios`, `consultar_socio`, `desactivar_socio` | `api-socios` | Administración de socios (HU-W01) | `ok`, `error` |
| `autenticar_socio` | `api-socios` | Verificación del socio tras el borde (HU-W01 AC2) | `ok`, `rechazado` |
| `cambiar_plan_socio`, `consultar_consumo_socio`, `listar_planes` | `api-socios` | Plan de uso y consumo de la cuota por socio (HU-W02) | `ok`, `error` |
| `medir_cuota_socios` | `api-socios` | Porcentaje de la cuota de cada socio activo para `PartnerQuotaUsage` (HU-W28) | `ok`, `error` |
| `socio.cotizar` | `api-socios` | Caso de uso `CotizarOferta` (HU-W04): `POST /cotizaciones` del socio, incluida la llamada a `cotizacion` (`POST /v1/cotizaciones` con el socio como actor) | `ok`, `rechazado` (rechazo de `cotizacion` con su código u oferta vencida al repetir la clave), `error` / `timeout` (`cotizacion` no respondió o violó su contrato: 503) |
| `socio.consultar_oferta` | `api-socios` | Caso de uso `ConsultarOferta` (HU-W04): `GET /cotizaciones/{cotizacion_id}` del socio | `ok`, `rechazado` (inexistente, de otro socio u oferta vencida: 404 o 410), `error` / `timeout` (503) |
| `consultar_alertas` | `bff-web` | Alertas abiertas, umbrales y resumen del mes (HU-W28) | `ok`, `error` |
| `cotizacion` | `cotizacion` | Caso de uso `calcular_prima` (HU-W03): de la solicitud a la cotización persistida. Meta p95 de 250 ms en `latency_targets` de `int` (HU-W28) | `ok`, `degradado` (HU-W06: oferta provisional, una fuente opcional del perfil faltó y una regla de fallback lo permitió), `pendiente` (HU-W06: 503 `DEPENDENCIA_NO_DISPONIBLE` con `perfil.fuentes`, sin oferta; el servicio está sano y no cuenta en `OperationErrors`: la falla ya la contó el proveedor en `consulta.<FUENTE>`), `rechazado` (4xx: tabla de decisión, datos, perfil, idempotencia o rol), `error` / `timeout` (5xx) |
| `reglas_suscripcion.consultar` | `catalogo` | Consulta de las reglas de suscripción vigentes o de una versión (HU-W13) | `ok`, `rechazado` (producto inexistente o sin reglas vigentes), `error` |
| `reglas_fallback.consultar` | `catalogo` | Consulta de las reglas de fallback vigentes o de una versión (HU-W06) | `ok`, `rechazado` (producto inexistente o sin reglas vigentes), `error` |
| `cotizacion.insumos_suscripcion` | `cotizacion` | Insumos de la oferta para la suscripción (HU-W13): cotización, perfil y estado de identidad; incluye `cotizacion.dependencia.clientes.identidad` | `ok`, `rechazado` (inexistente o rol no permitido), `error` / `timeout` (5xx) |
| `suscripcion.evaluar` | `polizas` | Caso de uso `solicitar_suscripcion` (HU-W13): de la solicitud a la decisión persistida con su fila de `outbox` | `aprobada`, `rechazada`, `revision` (decisión nueva o existente), `rechazado` (4xx: identidad, oferta vencida, cotización inexistente, sin reglas, idempotencia), `error` / `timeout` (5xx) |
| `suscripcion.dependencia.cotizacion.insumos`, `suscripcion.dependencia.catalogo.reglas` | `polizas` | Cada dependencia de la evaluación (HU-W13), en paralelo y sin conexión de base abierta; el "no existe" de una dependencia que respondió es `ok`. Desde HU-W14, `suscripcion.dependencia.catalogo.reglas` también mide la lectura de la versión de reglas de un rechazo para comunicar sus alternativas; ahí `error` o `timeout` no fallan la respuesta (alternativas vacías) | `ok`, `timeout`, `error` |
| `suscripcion.consultar` | `polizas`, `bff-web` | Lectura de una decisión o de la decisión de una cotización (HU-W13); en `bff-web` incluye el nombre del cliente. Desde HU-W14, la decisión leída trae su `comunicacion`, derivada al leer (en `polizas`, después de esta operación; ver `suscripcion.dependencia.catalogo.reglas`) | `ok`, `rechazado` (inexistente o rol no permitido), `error` / `timeout` (5xx, solo `bff-web`) |
| `suscripcion.bandeja`, `suscripcion.resumen` | `polizas`, `bff-web` | Bandeja de casos en revisión (E01) e indicadores del día (DS2-14) (HU-W13); en `bff-web` la bandeja compone `clienteNombre` desde `clientes` | `ok`, `rechazado` (filtros inválidos o rol no permitido), `error` / `timeout` (5xx, solo `bff-web`) |
| `suscripcion.reconstruir` | `polizas` | Reevaluación de una decisión con los insumos y la versión de reglas guardados (HU-W13) | `ok` (coincide), `diferente` (no coincide), `rechazado` (inexistente), `error` / `timeout` |
| `suscripcion.solicitar` | `bff-web` | Solicitud de la decisión de suscripción desde el portal (llamada a `polizas`, HU-W13); desde HU-W14 la respuesta trae la `comunicacion` | `ok`, `rechazado` (4xx de `polizas`), `error` / `timeout` |
| `aceptacion.registrar` | `polizas`, `bff-web` | Registro de la aceptación de la oferta (HU-W15): en `polizas`, caso de uso `RegistrarAceptacion`, de la solicitud a la aceptación persistida con su fila `OfertaAceptada` en el `outbox` (sin llamadas a otros servicios); en `bff-web`, la llamada a `polizas` | `ok` (registrada o repetida con la misma clave), `rechazado` (4xx: texto no vigente, decisión no aprobada o inexistente, oferta vencida o ya aceptada, declaraciones incompletas, idempotencia), `error` (5xx; también `timeout` en `bff-web`) |
| `aceptacion.consultar` | `polizas`, `bff-web` | Lectura de la aceptación de una cotización (HU-W15) | `ok`, `rechazado` (inexistente o rol no permitido), `error` / `timeout` (5xx, solo `bff-web`) |
| `emision` | `polizas` | Caso de uso `EmitirPolizas` (HU-W17, 17.8), la tarea emisora: una por póliza `EMITIENDO` procesada, desde que se toma hasta el commit (sin la espera en la cola ni entre ciclos). Sin documento: datos, PDF y `PutObject`; sin firma: `POST /v1/firmas` de `adaptador-firma` con la huella registrada; luego `ACTIVA` y `PolizaEmitida` v1 en el outbox, en una transacción y con la guarda. Meta p95 de `latency_targets` en `int` (1500 ms). Log `emision: póliza procesada` con `poliza_id`, `clave_recaudo`, `resultado` y `codigo`, con la correlación del recaudo y sin el cliente ni la huella | `ok` (`ACTIVA` y su evento), `ya_emitida` (otra tarea la activó: sin evento), `pendiente` (sigue `EMITIENDO` con su código: `DATOS_DOCUMENTO_*`, `DOCUMENTO_NO_GENERADO`, `ALMACEN_*`, `FIRMA_*` o `HUELLA_NO_COINCIDE`; se reintenta pasado `SOLVENTA_EMISION_REINTENTO_S`, 30 s; no suma en `OperationErrors`, con el log WARNING `emision: la emisión falló; sigue EMITIENDO y se reintenta`), `error` (fallo inesperado, p. ej. la base, con el código `EMISION_ERROR_INESPERADO`; o una `pendiente` con más de `SOLVENTA_EMISION_ALERTA_TRAS_S`, 3600 s, desde su creación, con el log ERROR `emision: póliza EMITIENDO vencida; se sigue reintentando` y `antiguedad_s`). Sin `timeout` ni `rechazado`: un timeout de la firma es `pendiente`. Sigue `EMITIENDO` y sin tope |
| `consulta` | `consulta` | Prevista (HU-W18, HU-M10): lectura de la proyección. Meta p95 de `latency_targets` en `int` | `ok`, `error`, `rechazado` (no encontrada o ajena) |
| `cobro.pagos` | `adaptador-pagos` | Caso de uso `Cobrar` (llamada a la pasarela de pagos, aliado `pagos`, HU-W16): `POST /v1/cobros`, sin reintentos | `ok`, `timeout`, `error`, `rechazado` (pago rechazado por el proveedor) |
| `conciliacion.pagos` | `adaptador-pagos` | Caso de uso `ConsultarCobro` (consulta del estado de un cobro en la pasarela de pagos, aliado `pagos`, HU-W16): `GET /v1/cobros/{claveRecaudo}`, que invoca la conciliación de `dinero` (16.11) para resolver un movimiento `PENDIENTE` | `ok` (`APROBADO` o `RECHAZADO`), `timeout`, `error`, `rechazado` (cobro no encontrado: `COBRO_NO_ENCONTRADO`) |
| `recaudo.conciliar` | `dinero` | Caso de uso `ConciliarRecaudos` (HU-W16, 16.11), una por movimiento `PENDIENTE` antiguo: consulta a `adaptador-pagos` (`GET /v1/cobros/{claveRecaudo}?pagadorRef`) y, si el cobro no existe, reintenta `POST /v1/cobros` con la misma `claveRecaudo`; aplica el resultado con la guarda de `PENDIENTE` y su evento en el outbox. Log `conciliacion: movimiento conciliado` con `recaudo_id`, `clave_recaudo`, `resultado`, `codigo` y `reintento`, con la correlación del movimiento y sin el pagador | `confirmado`, `rechazado` (el proveedor rechazó el cobro), `incierto` (sigue `PENDIENTE`; `codigo` del adaptador), `ya_resuelto` (otra tarea lo resolvió: sin evento), `error` / `timeout` (falla inesperada, p. ej. la base). Un `incierto` con más de `SOLVENTA_CONCILIACION_ALERTA_TRAS_S` (3600 s) desde su creación se registra como `error`, para que sume en `OperationErrors` y lo vea la alarma de tasa de error de `dinero`, con el log ERROR `conciliacion: movimiento PENDIENTE vencido; se sigue conciliando` (`recaudo_id`, `clave_recaudo`, `codigo`, `antiguedad_s`). Sigue `PENDIENTE` y sin tope |
| `firma.documento` | `adaptador-firma` | Caso de uso `Firmar` (firma de la huella sha256 del documento de póliza con el proveedor de firma, aliado `firma`, HU-W17): `POST /v1/firmas`, con `documentoId` como `Idempotency-Key` y sin reintentos | `ok`, `timeout`, `error` (también la firma de otro documento u otra huella). Sin `rechazado`: el proveedor no rechaza firmas (plan W17, §b, decisión 9) |
| `notificacion.push` | `adaptador-firma` | Prevista (HU-M11): envío de un aviso push al proveedor (aliado `push`) | `ok`, `timeout`, `error`, `rechazado` (token de dispositivo inválido) |
| `aviso.push` | `avisos` | Prevista (HU-M11): envío de un aviso a un dispositivo (llamada a `adaptador-firma`) | `ok`, `timeout`, `error`, `rechazado` |

`<FUENTE>` ∈ {`FA_PRODUCTOS_VIGENTES`, `FA_HISTORIAL_PAGOS_12M`, `FA_INGRESOS_AGREGADOS`, `DA_REGISTROS_PUBLICOS`}.

**Dependencia que agotó su tiempo (HU-W05):** `logger.warning("dependencia agotó su tiempo", extra={"proveedor": …, "operacion": …, "timeout_ms": …, "restante_ms": …})` (logger `solventa.resiliencia`), con el `correlation_id` que agrega el formateador. Lo emiten `SinProteccion` y `ProteccionDependencia` en todos los servicios que las usan (`cotizacion`, `adaptador-datos`, `adaptador-identidad`, `api-socios`, `adaptador-pagos`). `proveedor` es el grupo del bulkhead (`catalogo`, `clientes`, `base-cotizacion`, el aliado o `cotizacion`); `operacion`, la dependencia o la fuente. Los spans de dependencia llevan `solventa.timeout_ms` (timeout efectivo), `solventa.restante_ms` (deadline recibido) y `solventa.presupuesto_ms` (presupuesto del recorrido); el span de `cotizacion` y el de `socio.cotizar` llevan `solventa.presupuesto_ms`.

**Log de cierre de la cotización (HU-W03):** `logger.info("cotizacion calculada", extra={"cotizacion_id": …, "producto_id": …, "version_reglas": …, "perfil_id": …, "dependencias": [{"nombre": …, "ms": …, "resultado": …}]})`, solo identificadores: ni montos, ni edad, ni fecha de nacimiento. Desde HU-W06 agrega `fallback` (null en el recorrido nominal): `{"decision": "DEGRADADA", "decision_id": …, "version_reglas": …, "faltantes": [{"fuente": …, "dato": …, "motivo": …, "regla": …}]}`, solo nombres y códigos. Una cotización pendiente registra `logger.warning("cotizacion pendiente", extra={"decision_id": …, "perfil_id": …, "producto_id": …, "fallback": {…}})`.

**Log de cierre de la suscripción (HU-W13):** `logger.info("suscripcion decidida", extra={"decision_id": …, "resultado": …, "version_reglas": …, "dependencias": [{"nombre": …, "ms": …, "resultado": …}]})`, solo identificadores: ni insumos, ni montos, ni edad.

**Log de cierre de la aceptación (HU-W15):** `logger.info("aceptacion registrada", extra={"aceptacion_id": …, "decision_id": …, "version_declaraciones": …})`, solo identificadores: ni prima, ni suma asegurada, ni declaraciones.

**Aviso de alternativas no disponibles (HU-W14):** `logger.warning("alternativas no disponibles", extra={"decision_id": …, "version_reglas": …, "dependencia": "catalogo", "resultado": "error" | "timeout"})` cuando `catalogo` no entrega la versión de reglas de un rechazo; la decisión se comunica con `alternativas = []`.

**Log de cierre del perfilamiento:** `logger.info("perfil calculado", extra={"perfil_id": …, "estado": …, "dependencias": [{"nombre": …, "ms": …, "resultado": …}]})`, dentro de la `operacion`. Las dependencias van en una **lista** para que `sanitizar` no redacte valores por el nombre de la clave.

## Degradación por proveedor (HU-W06)

La alarma `<prefijo>-proveedor-<aliado>-degradado` (`infra/apps/alarms.tf`, variable `provider_operations`) mide, por periodo de `alarm_period_seconds` (60 s):

```
tasa (%) = 100 × Σ Sum(OperationErrors) / Σ SampleCount(OperationDuration)
           sobre las operaciones del proveedor, con dimensiones Environment, Service y Operation
```

Abre la alerta con `tasa > provider_error_percent` (20 % en `int`) en 3 de 5 periodos; sin llamadas queda en OK. `OperationErrors` cuenta los resultados `error` y `timeout`; `rechazado` no es una falla del proveedor.

Con los modos del simulador, `consulta.<FUENTE>` de `adaptador-datos` queda: `caido` (503) → `error`, `malformada` (RESPUESTA_INVALIDA) → `error` y `lento` → `timeout` (prueba `test_metrica_y_log_con_resultado`). Los tres cuentan en la alarma del aliado. Del lado de `cotizacion`, la cotización que sigue con una fuente faltante queda `degradado` o `pendiente` y no cuenta en `OperationErrors`: la falla se alarma una sola vez, en el proveedor.

| Aliado | Servicio | Operaciones |
| --- | --- | --- |
| `open-finance` | `adaptador-datos` | `consulta.FA_PRODUCTOS_VIGENTES`, `consulta.FA_HISTORIAL_PAGOS_12M`, `consulta.FA_INGRESOS_AGREGADOS` |
| `datos-abiertos` | `adaptador-datos` | `consulta.DA_REGISTROS_PUBLICOS` |
| `kyc` | `adaptador-identidad` | `verificacion.kyc` |
| `pagos` | `adaptador-pagos` | `cobro.pagos` |
| `firma` | `adaptador-firma` | `firma.documento` |
| `push` | `adaptador-firma` | `notificacion.push` |

Un adaptador que agregue una operación hacia su proveedor debe agregarla a `provider_operations` en `infra/apps/variables.tf` (y en `apps.tfvars` si el ambiente la sobrescribe). La alarma `<prefijo>-avisos-push-fallos` cuenta `Sum(OperationErrors)` de `avisos`/`aviso.push` en 5 minutos (`push_failure_count`).
