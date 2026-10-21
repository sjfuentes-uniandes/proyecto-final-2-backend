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
| `habilitar_socio`, `listar_socios`, `consultar_socio`, `desactivar_socio` | `api-socios` | Administración de socios (HU-W01) | `ok`, `error` |
| `autenticar_socio` | `api-socios` | Verificación del socio tras el borde (HU-W01 AC2) | `ok`, `rechazado` |
| `cambiar_plan_socio`, `consultar_consumo_socio`, `listar_planes` | `api-socios` | Plan de uso y consumo de la cuota por socio (HU-W02) | `ok`, `error` |
| `medir_cuota_socios` | `api-socios` | Porcentaje de la cuota de cada socio activo para `PartnerQuotaUsage` (HU-W28) | `ok`, `error` |
| `consultar_alertas` | `bff-web` | Alertas abiertas, umbrales y resumen del mes (HU-W28) | `ok`, `error` |
| `cotizacion` | `cotizacion` | Prevista: meta p95 de `latency_targets` en `int` (HU-W28). Sin datos, su alarma queda en OK | — |
| `emision` | `polizas` | Prevista (HU-W17): emisión de la póliza tras el recaudo. Meta p95 de `latency_targets` en `int` | `ok`, `error`, `timeout`, `rechazado` |
| `consulta` | `consulta` | Prevista (HU-W18, HU-M10): lectura de la proyección. Meta p95 de `latency_targets` en `int` | `ok`, `error`, `rechazado` (no encontrada o ajena) |
| `cobro.pagos` | `adaptador-pagos` | Prevista (HU-W16): llamada a la pasarela de pagos (aliado `pagos`) | `ok`, `timeout`, `error`, `rechazado` (pago rechazado por el proveedor) |
| `firma.documento` | `adaptador-firma` | Prevista (HU-W17): firma del documento de póliza (aliado `firma`) | `ok`, `timeout`, `error`, `rechazado` |
| `notificacion.push` | `adaptador-firma` | Prevista (HU-M11): envío de un aviso push al proveedor (aliado `push`) | `ok`, `timeout`, `error`, `rechazado` (token de dispositivo inválido) |
| `aviso.push` | `avisos` | Prevista (HU-M11): envío de un aviso a un dispositivo (llamada a `adaptador-firma`) | `ok`, `timeout`, `error`, `rechazado` |

`<FUENTE>` ∈ {`FA_PRODUCTOS_VIGENTES`, `FA_HISTORIAL_PAGOS_12M`, `FA_INGRESOS_AGREGADOS`, `DA_REGISTROS_PUBLICOS`}.

**Log de cierre del perfilamiento:** `logger.info("perfil calculado", extra={"perfil_id": …, "estado": …, "dependencias": [{"nombre": …, "ms": …, "resultado": …}]})`, dentro de la `operacion`. Las dependencias van en una **lista** para que `sanitizar` no redacte valores por el nombre de la clave.

## Degradación por proveedor (HU-W06)

La alarma `<prefijo>-proveedor-<aliado>-degradado` (`infra/apps/alarms.tf`, variable `provider_operations`) mide, por periodo de `alarm_period_seconds` (60 s):

```
tasa (%) = 100 × Σ Sum(OperationErrors) / Σ SampleCount(OperationDuration)
           sobre las operaciones del proveedor, con dimensiones Environment, Service y Operation
```

Abre la alerta con `tasa > provider_error_percent` (20 % en `int`) en 3 de 5 periodos; sin llamadas queda en OK. `OperationErrors` cuenta los resultados `error` y `timeout`; `rechazado` no es una falla del proveedor.

| Aliado | Servicio | Operaciones |
| --- | --- | --- |
| `open-finance` | `adaptador-datos` | `consulta.FA_PRODUCTOS_VIGENTES`, `consulta.FA_HISTORIAL_PAGOS_12M`, `consulta.FA_INGRESOS_AGREGADOS` |
| `datos-abiertos` | `adaptador-datos` | `consulta.DA_REGISTROS_PUBLICOS` |
| `kyc` | `adaptador-identidad` | `verificacion.kyc` |
| `pagos` | `adaptador-pagos` | `cobro.pagos` |
| `firma` | `adaptador-firma` | `firma.documento` |
| `push` | `adaptador-firma` | `notificacion.push` |

Un adaptador que agregue una operación hacia su proveedor debe agregarla a `provider_operations` en `infra/apps/variables.tf` (y en `apps.tfvars` si el ambiente la sobrescribe). La alarma `<prefijo>-avisos-push-fallos` cuenta `Sum(OperationErrors)` de `avisos`/`aviso.push` en 5 minutos (`push_failure_count`).
