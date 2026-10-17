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
| `ROL_NO_AUTORIZADO` | 403 | `cotizacion`, `catalogo`, `clientes`, `bff-web`, `bff-movil` | El rol o el grupo no permite la operación; en `clientes`, `X-Actor-Rol` distinto de `CLIENTE`; en `bff-movil`, el token es de otro pool de Cognito |
| `IDENTIDAD_NO_VERIFICADA` | 403 | `cotizacion`, `clientes` | La identidad del cliente fue rechazada; en `clientes`, otorgar un consentimiento exige identidad `VERIFICADO` (HU-M08) |
| `IDENTIDAD_PENDIENTE` | 409 | `cotizacion` | La verificación de identidad no terminó |
| `CLIENTE_NO_ENCONTRADO` | 404 | `cotizacion` (y el stub de `clientes`), `clientes`, `bff-movil` | El cliente no existe; en `clientes`, también si el titular aún no inició su verificación |
| `PERFIL_NO_ENCONTRADO` | 404 | `cotizacion`, `bff-web` | El perfil no existe |
| `PRODUCTO_NO_DISPONIBLE` | 422 | `cotizacion` | Producto distinto de `vida-hipotecario` |
| `IDEMPOTENCIA_CONFLICTO` | 422 | `cotizacion`, `clientes` | Misma `Idempotency-Key` con otro cuerpo; en `clientes`, la clave es por cliente y se conserva 24 h (HU-M08) |
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
| `DEPENDENCIA_NO_DISPONIBLE` | 503 | `cotizacion`, `clientes` | `clientes` o `catalogo` no respondieron o violaron su contrato (`cotizacion`); `adaptador-identidad` no respondió y la verificación no terminó, es recuperable (`clientes`, `detalles.motivo`) |
| `SERVICIO_NO_DISPONIBLE` | 503 | `bff-web`, `bff-movil` | `cotizacion` (`bff-web`) o `clientes` (`bff-movil`) no respondieron |
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
| `CONSENTIMIENTO_NO_ENCONTRADO` | 404 | `clientes` | El consentimiento no existe o es de otro cliente: no se distingue, para no revelar su existencia (HU-M08) |
| `CONSENTIMIENTO_YA_VIGENTE` | 409 | `clientes` | Ya hay un consentimiento `ACTIVO` y vigente para el propósito; para cambiar alcances o vigencia se revoca y se otorga de nuevo (`detalles.consentimientoId`, HU-M08) |
| `VIGENCIA_INVALIDA` | 422 | `clientes` | `vigenciaMeses` no es una de las permitidas (6 o 12, ADR 0005) (HU-M08) |
| `TEXTO_NO_VIGENTE` | 422 | `clientes` | `versionTexto` no es la del texto vigente del propósito e idioma (ADR 0005) (HU-M08) |
| `PLAN_NO_DISPONIBLE` | 422 | `api-socios`, `bff-web` | El plan no está en `PARTNERS_USAGE_PLANS` del ambiente (`detalles.plan`, HU-W02) |
| `NO_ENCONTRADO` | 404 | Todos | Ruta inexistente (manejador global) o `HTTPException` 404 en `RutaB2`. `api-socios` también lo usa en `/admin/*` cuando la solicitud llega del API de socios, para no revelar la ruta |
| `METODO_NO_PERMITIDO` | 405 | Todos | Método no soportado por la ruta |
| `ERROR_HTTP` | otros | Todos | Cualquier otro `HTTPException` |
| `ERROR_INTERNO` | 500 | Todos | Error no controlado; solo se registra el tipo |
