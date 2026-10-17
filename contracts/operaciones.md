# Operaciones medidas

Nombres de `Operation` en `OperationDuration` y `OperationErrors` (`solventa_common.telemetry.operacion`). El colector exporta `^Operation.*` con la dimensión `Operation`; no se crean métricas nuevas.

| Operación (`Operation`) | Servicio | Dónde | Resultados |
| --- | --- | --- | --- |
| `perfilamiento` | `cotizacion` | Caso de uso `crear_perfil` | `ok` (CALCULADO), `degradado` (DEGRADADO), `insuficiente` (INFORMACION_INSUFICIENTE), `rechazado` (4xx), `error` / `timeout` (5xx) |
| `perfilamiento.recalculo` | `cotizacion` | Caso de uso `recalcular_perfil` | Igual que `perfilamiento` |
| `perfilamiento.dependencia.clientes.identidad` | `cotizacion` | Llamada a la identidad del cliente | `ok`, `timeout`, `error` |
| `perfilamiento.dependencia.clientes.consentimiento` | `cotizacion` | Verificación del consentimiento | `ok`, `timeout`, `error` |
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
| `consentimientos.texto_vigente` | `bff-movil` | Caso de uso `ConsultarTextoVigente` (HU-M08) | `ok`, `timeout`, `error`, `rechazado` (propósito o idioma no soportados) |
| `modelo_riesgo.publicar`, `modelo_riesgo.consultar` | `catalogo` | Casos de uso del modelo | — |
| `habilitar_socio`, `listar_socios`, `consultar_socio`, `desactivar_socio` | `api-socios` | Administración de socios (HU-W01) | `ok`, `error` |
| `autenticar_socio` | `api-socios` | Verificación del socio tras el borde (HU-W01 AC2) | `ok`, `rechazado` |
| `cambiar_plan_socio`, `consultar_consumo_socio`, `listar_planes` | `api-socios` | Plan de uso y consumo de la cuota por socio (HU-W02) | `ok`, `error` |
| `medir_cuota_socios` | `api-socios` | Porcentaje de la cuota de cada socio activo para `PartnerQuotaUsage` (HU-W28) | `ok`, `error` |
| `consultar_alertas` | `bff-web` | Alertas abiertas, umbrales y resumen del mes (HU-W28) | `ok`, `error` |
| `cotizacion`, `emision`, `consulta` | `cotizacion` | Previstas: metas p95 de `latency_targets` en `int` (HU-W28). Sin datos, sus alarmas quedan en OK | — |

`<FUENTE>` ∈ {`FA_PRODUCTOS_VIGENTES`, `FA_HISTORIAL_PAGOS_12M`, `FA_INGRESOS_AGREGADOS`, `DA_REGISTROS_PUBLICOS`}.

**Log de cierre del perfilamiento:** `logger.info("perfil calculado", extra={"perfil_id": …, "estado": …, "dependencias": [{"nombre": …, "ms": …, "resultado": …}]})`, dentro de la `operacion`. Las dependencias van en una **lista** para que `sanitizar` no redacte valores por el nombre de la clave.
