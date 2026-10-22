# Códigos de error

Formato estable de todo error de las rutas nuevas (`solventa_common.errors`):

```json
{"codigo": "MAYUSCULAS", "mensaje": "texto genérico", "correlationId": "…", "detalles": {}}
```

- Los clientes (web, móvil y socios) traducen por `codigo`, nunca por `mensaje`.
- El 422 de validación es `SOLICITUD_INVALIDA`, con `detalles.campos` = lista de rutas de los campos inválidos (nunca sus valores).
- Las rutas de HU-W27 en `bff-web` conservan `{"detail"}` hasta que se migren a este formato.

| Código | HTTP | Emisor | Cuándo |
| --- | --- | --- | --- |
| `SOLICITUD_INVALIDA` | 422 | Todos | Cuerpo, consulta o encabezado inválidos (`detalles.campos`). En `RutaB2`, también un `HTTPException` 400 |
| `ACTOR_REQUERIDO` | 401 | `cotizacion`, `catalogo`, `clientes` | Falta `X-Actor-Id` o `X-Actor-Rol` |
| `ROL_NO_AUTORIZADO` | 403 | `cotizacion`, `catalogo`, `clientes`, `bff-web`, `bff-movil` | El rol o el grupo no permite la operación; en `clientes`, `X-Actor-Rol` distinto de `CLIENTE`; en `bff-movil`, el token es de otro pool de Cognito. En `/v1/cotizaciones` (HU-W03): rol distinto de `ASESOR` o `SOCIO`, un `SOCIO` cuyo `X-Actor-Id` no es `SOC-NNNNNN`, o un `SOCIO` que envía `perfilId` u otro `socioId` |
| `IDENTIDAD_NO_VERIFICADA` | 403 | `cotizacion`, `clientes`, `bff-movil` | La identidad del cliente fue rechazada; en `clientes`, otorgar un consentimiento exige identidad `VERIFICADO` (HU-M08); `bff-movil` lo reenvía tal cual |
| `IDENTIDAD_PENDIENTE` | 409 | `cotizacion` | La verificación de identidad no terminó |
| `CLIENTE_NO_ENCONTRADO` | 404 | `cotizacion` (y el stub de `clientes`), `clientes`, `bff-movil`, `bff-web` | El cliente no existe; en `clientes`, también si el titular aún no inició su verificación; `bff-web` lo transmite de `clientes` (HU-W30 y lista de consentimientos de HU-W31) |
| `PERFIL_NO_ENCONTRADO` | 404 | `cotizacion`, `bff-web` | El perfil no existe; también el `perfilId` de una cotización (HU-W03) |
| `PRODUCTO_NO_DISPONIBLE` | 422 | `cotizacion` | Perfilamiento (`/v1/perfiles`): el producto no tiene un modelo de riesgo vigente en `catalogo`. Se conserva para el perfil; la cotización (HU-W03) usa los cuatro códigos siguientes, más precisos |
| `PRODUCTO_NO_ENCONTRADO` | 422 / 404 | `cotizacion`, `catalogo`, `bff-web` | No existe el producto. **422** en `POST /v1/cotizaciones`: el producto es un campo del cuerpo, no el recurso de la ruta, y AC2 agrupa inexistente, inactivo y vencido como un rechazo funcional, así que el socio maneja los cuatro códigos de producto con un mismo estado; **404** cuando el producto es el recurso de la ruta: `GET /v1/productos/{productoId}/condiciones` (`cotizacion`) y `GET /v1/productos/{productoId}...` (`catalogo`). HU-W03 AC2 |
| `PRODUCTO_INACTIVO` | 422 | `cotizacion`, `bff-web` | El producto existe pero su estado no es `ACTIVO` (HU-W03 AC2) |
| `PRODUCTO_FUERA_DE_VIGENCIA` | 422 | `cotizacion`, `bff-web` | La fecha de la cotización no cae en la vigencia `[vigenteDesde, vigenteHasta)` del producto (HU-W03 AC2) |
| `REGLAS_NO_VIGENTES` | 422 / 404 | `cotizacion`, `catalogo`, `bff-web` | El producto (activo y vigente) no tiene reglas de prima vigentes en la fecha. 404 en `catalogo` (`.../reglas-prima/vigente` o una versión inexistente), 422 en `cotizacion` (HU-W03 AC2) |
| `DATOS_FUERA_DE_RANGO` | 422 | `cotizacion`, `bff-web` | Suma asegurada, plazo o edad fuera de los límites de las reglas del producto (`detalles.campos`: `datosAsegurado.sumaAsegurada`, `datosAsegurado.plazoCreditoMeses`, `datosAsegurado.fechaNacimiento`; nunca sus valores). HU-W03 |
| `COBERTURA_NO_DISPONIBLE` | 422 | `cotizacion`, `bff-web` | Una cobertura pedida no es opcional en el producto (`detalles.campos`: `coberturasOpcionales`). HU-W03 |
| `PERFIL_NO_CORRESPONDE` | 422 | `cotizacion`, `bff-web` | El perfil de la cotización es de otro cliente u otro producto, o se calculó con otros datos declarados (`detalles.campos`). HU-W03 |
| `COTIZACION_NO_ENCONTRADA` | 404 | `cotizacion`, `bff-web` | La cotización no existe o es de otro socio: no se distingue, para no revelar su existencia. HU-W03 |
| `IDEMPOTENCIA_CONFLICTO` | 422 | `cotizacion`, `clientes`, `bff-movil` | Misma `Idempotency-Key` con otro cuerpo; en `clientes`, la clave es por cliente y se conserva 24 h (HU-M08); `bff-movil` lo reenvía tal cual. En `/v1/cotizaciones` la clave es por actor (HU-W03) |
| `SIN_PERFIL_ANTERIOR` | 422 | `bff-web` | Comparación sin perfil anterior |
| `MODELO_NO_ENCONTRADO` | 404 | `catalogo` | No hay modelo vigente o con esa versión |
| `VERSION_EXISTENTE` | 409 | `catalogo` | La versión del modelo ya existe |
| `DEFINICION_INVALIDA` | 422 | `catalogo` | La definición del modelo no cumple sus validaciones |
| `VIGENCIA_RETROACTIVA` | 422 | `catalogo` | Vigencia en el pasado |
| `CAMPOS_NO_SOPORTADOS` | 422 | `adaptador-datos` | Algún campo no pertenece a la fuente (`detalles.campos`) |
| `CONSENTIMIENTO_REQUERIDO` | 422 | `adaptador-datos` | Falta `consentimientoId`; no se llama al proveedor |
| `RESPUESTA_INVALIDA` | 502 | `adaptador-datos`, `adaptador-identidad` | El proveedor respondió con un formato inválido o un 4xx |
| `FUENTE_NO_DISPONIBLE` | 503 | `adaptador-datos`, `adaptador-identidad` | Proveedor caído, 5xx o protección abierta (`detalles.motivo`, `detalles.circuito`) |
| `TIEMPO_AGOTADO` | 504 | `adaptador-datos`, `adaptador-identidad` | El proveedor no respondió dentro del timeout efectivo |
| `DEPENDENCIA_NO_DISPONIBLE` | 503 | `cotizacion`, `clientes` | `clientes` o `catalogo` no respondieron o violaron su contrato (`cotizacion`; en la cotización `detalles.dependencia` es `catalogo.producto`, `catalogo.reglas_prima`, `clientes.identidad` o `perfil`); `adaptador-identidad` no respondió y la verificación no terminó, es recuperable (`clientes`, `detalles.motivo`) |
| `SERVICIO_NO_DISPONIBLE` | 503 | `bff-web`, `bff-movil` | `cotizacion` o `clientes` (`bff-web`) o `clientes` (`bff-movil`) no respondieron; en `bff-web`, en la lectura de consentimientos (HU-W31), también cuando `clientes` responde un código que la ruta no promete, cualquier 5xx o un cuerpo fuera de su contrato (falla cerrado, sin `detalles`); en `bff-movil`, también cuando `clientes` responde `DEPENDENCIA_NO_DISPONIBLE`, otro 5xx o una respuesta fuera de su contrato (falla cerrado, sin `detalles`) |
| `NO_AUTENTICADO` | 401 | `bff-web` (`RutaB2`), `bff-movil` | `HTTPException` 401 en un router con `RutaB2` (`bff-web`) o `ErrorNegocio` tras revalidar el JWT (`bff-movil`) |
| `NO_ENCONTRADO` | 404 | Todos | Ruta inexistente (manejador global) o `HTTPException` 404 en `RutaB2` |
| `SERVICIO_NO_DISPONIBLE` | 503 | `bff-web` | `cotizacion` o `api-socios` no respondieron |
| `NO_AUTENTICADO` | 401 | `bff-web` (`RutaB2`), `api-socios` | `HTTPException` 401 dentro de un router con `RutaB2`. En `api-socios`: credenciales de socio inexistentes, incorrectas, desactivadas o con API key de otro socio (HU-W01 AC2) |
| `PERMISO_NO_ASIGNADO` | 403 | `api-socios` | El socio no tiene el permiso de la operación (`detalles.permiso`, HU-W01 AC4) |
| `SOCIO_NO_ENCONTRADO` | 404 | `api-socios`, `bff-web` | El socio no existe |
| `OPERACION_NO_DISPONIBLE` | 501 | `api-socios` | Operación de socio autorizada pero aún no implementada |
| `SOCIO_DESACTIVADO` | 409 | `api-socios`, `bff-web` | No se puede cambiar el plan de un socio desactivado (HU-W02) |
| `REGISTRO_CORREO_DISTINTO` | 409 | `clientes`, `bff-movil` | El registro ya se verificó con otro correo: la verificación va ligada a un correo (HU-M06) |
| `REGISTRO_YA_VINCULADO` | 409 | `clientes`, `bff-movil` | La cuenta de ese registro ya existe, o el titular ya pertenece a otro cliente (HU-M06) |
| `TOKEN_REGISTRO_INVALIDO` | 403 | `clientes`, `bff-movil` | El token de registro no sirve (`detalles.motivo`: `FORMATO`, `FIRMA`, `VENCIDO`, `REGISTRO`, `CORREO`, `CLIENTE`, `NO_VERIFICADO`) (HU-M06) |
| `CONSENTIMIENTO_NO_ENCONTRADO` | 404 | `clientes`, `bff-movil`, `bff-web` | El consentimiento no existe o es de otro cliente: no se distingue, para no revelar su existencia (HU-M08). En `clientes`, también al revocar y al consultar el historial (HU-M09); revocar uno ya `REVOCADO` o `VENCIDO` no es error: responde 200 con su estado |
| `CONSENTIMIENTO_YA_VIGENTE` | 409 | `clientes`, `bff-movil` | Ya hay un consentimiento `ACTIVO` y vigente para el propósito; para cambiar alcances o vigencia se revoca y se otorga de nuevo (`detalles.consentimientoId`, HU-M08) |
| `VIGENCIA_INVALIDA` | 422 | `clientes`, `bff-movil` | `vigenciaMeses` no es una de las permitidas (6 o 12, ADR 0005) (HU-M08) |
| `TEXTO_NO_VIGENTE` | 422 | `clientes`, `bff-movil` | `versionTexto` no es la del texto vigente del propósito e idioma (ADR 0005) (HU-M08) |
| `PLAN_NO_DISPONIBLE` | 422 | `api-socios`, `bff-web` | El plan no está en `PARTNERS_USAGE_PLANS` del ambiente (`detalles.plan`, HU-W02) |
| `NO_ENCONTRADO` | 404 | Todos | Ruta inexistente (manejador global) o `HTTPException` 404 en `RutaB2`. `api-socios` también lo usa en `/admin/*` cuando la solicitud llega del API de socios, para no revelar la ruta |
| `METODO_NO_PERMITIDO` | 405 | Todos | Método no soportado por la ruta |
| `ERROR_HTTP` | otros | Todos | Cualquier otro `HTTPException` |
| `ERROR_INTERNO` | 500 | Todos | Error no controlado; solo se registra el tipo |
