# bff-movil

BFF de la aplicación móvil. Compone las respuestas de los servicios de dominio para el canal
`/movil` (`infra/platform/edge.tf`); no reimplementa reglas de negocio.

## Módulos

| Módulo | Historia | Qué hace |
| --- | --- | --- |
| `sesion` | HU-M07 | `GET /v1/sesion`: revalida el JWT del pool `clientes` de Cognito y devuelve los servicios autorizados del cliente según su identidad |
| `verificacion` | HU-M06 | `POST /v1/identidad/verificaciones`: revalida el JWT y envía la selfie y la prueba de vida a `clientes`, que verifica la identidad con el proveedor KYC |

## `GET /v1/sesion` (HU-M07)

Contrato: [`contracts/openapi/bff-movil.yaml`](../../contracts/openapi/bff-movil.yaml).

1. Revalida el `Authorization: Bearer <token>` contra el JWKS del emisor (`JWT_ISSUERS`):
   firma, `exp`, `token_use == "access"`, que `sub` sea igual a `X-Authenticated-Sub` y que el
   emisor sea el pool `clientes`. Un token de otro pool (por ejemplo, `backoffice`) recibe 403
   `ROL_NO_AUTORIZADO`; cualquier otro rechazo es 401 `NO_AUTENTICADO`.
2. Resuelve el `clienteId` del titular del token (`sub`) con `GET /v1/clientes/actual` en
   `clientes` (`ResolutorClienteHttp`). Un titular que aún no inició su verificación no tiene
   cliente: 404 `CLIENTE_NO_ENCONTRADO`.
3. Consulta `GET /v1/clientes/{clienteId}/identidad` en `clientes`, con `X-Actor-Id=<sub>` y
   `X-Actor-Rol=CLIENTE`. Sin reintentos: un timeout nunca se repite.
4. Traduce el estado de identidad a los servicios autorizados (catálogo provisional,
   `domain/sesion/modelos.py`):
   - `VERIFICADO` → `POLIZAS`, `SINIESTROS`, `PRIVACIDAD`.
   - `PENDIENTE` o `RECHAZADO` → `VERIFICACION_IDENTIDAD`.

### Limitación conocida: revocación del refresh token (AC4)

Cognito revoca el *refresh token* cuando el cliente cierra sesión o se revoca el acceso
(`enable_token_revocation` en `infra/platform/identity.tf`). Pero el *access token* sigue
siendo **criptográficamente válido hasta su `exp`** (30 minutos en el pool `app-movil`):
Cognito no expone un endpoint que este servicio pueda consultar por solicitud sin degradar la
latencia de cada llamada, y no se agregó esa consulta sin confirmación explícita (ver el plan
de la historia). En la práctica, AC4 se cumple en el peor caso dentro de esos 30 minutos: la
app pierde la capacidad de renovar la sesión, pero un access token ya emitido sigue
aceptándose hasta que expira. Si el negocio necesita invalidación inmediata, hace falta otra
historia que agregue la verificación activa (por ejemplo, `GetUser` de Cognito) o que acorte
la vigencia del access token.

## `POST /v1/identidad/verificaciones` (HU-M06)

Contrato: [`contracts/openapi/bff-movil.yaml`](../../contracts/openapi/bff-movil.yaml). Cuerpo:
`{"selfie": "<base64>", "pruebaDeVida": {…}}` (la selfie, de hasta 5 000 000 caracteres).

1. Revalida el JWT igual que `GET /v1/sesion`; el 401/403 se resuelve antes de validar el cuerpo.
2. Envía la evidencia a `POST /v1/clientes/actual/identidad/verificaciones` en `clientes`, con
   `X-Actor-Id=<sub>` y `X-Actor-Rol=CLIENTE`: el resultado siempre se asocia al titular del
   token, nunca a un dato del cuerpo. Sin reintentos: un timeout nunca se repite.
3. Responde `{clienteId, estado, resultado, motivo, verificadoEn}`:
   - `APROBADO` → `estado = VERIFICADO` (AC1).
   - `CAPTURA_INVALIDA` → 200 con `estado = PENDIENTE` y el `motivo`: la app explica cómo repetir
     la captura y el cliente **no** queda rechazado (AC2).
   - `RECHAZADO` → `estado = RECHAZADO`.
   - La verificación no terminó (`clientes` o el proveedor no respondieron) → 503
     `SERVICIO_NO_DISPONIBLE`: la app informa y reintenta; `clientes` reutiliza el mismo cliente
     del `sub`, así que no se crea un segundo cliente (AC3).
4. La selfie y la prueba de vida solo se reenvían: no se guardan ni se registran en logs ni
   trazas (AC4, parte del backend; la limpieza de la galería y del almacenamiento local es de la
   app).

El timeout de esta llamada (`SOLVENTA_VERIFICACION_TIMEOUT_MS`, 5 s) no usa el tope de 700 ms de
`solventa_common.resiliencia`: la verificación incluye la consulta al proveedor KYC. Los
timeouts de la cadena están anidados (KYC 3 s < `clientes`→`adaptador-identidad` 4 s <
`bff-movil`→`clientes` 5 s) para que el nivel de afuera nunca se rinda antes que el de adentro.

## Configuración

Además de lo común (`solventa_common.settings.ServiceSettings`):

| Variable | Alias sin prefijo | Por defecto | Qué es |
| --- | --- | --- | --- |
| `SOLVENTA_CLIENTES_URL` | `CLIENTES_URL` | `http://localhost:8083` | Base URL de `clientes` |
| `SOLVENTA_JWT_ISSUERS` | `JWT_ISSUERS` | `{}` | Emisor (`iss`) por pool de Cognito, JSON `{"customers": "...", "backoffice": "..."}` |
| `SOLVENTA_CLIENTES_TIMEOUT_MS` | — | `500` | Timeout de la llamada a `clientes` (tope real 700 ms, `solventa_common.resiliencia`) |
| `SOLVENTA_VERIFICACION_TIMEOUT_MS` | — | `5000` | Timeout de la verificación biométrica en `clientes`. No aplica el tope de 700 ms: incluye al proveedor KYC (3 s) y `clientes` espera 4 s al adaptador |
| `SOLVENTA_JWKS_TTL_S` | — | `3600` | Vigencia de las claves del emisor en caché |
| `SOLVENTA_JWKS_TIMEOUT_MS` | — | `500` | Timeout de la consulta al JWKS |
| `SOLVENTA_JWKS_REFRESCO_MIN_S` | — | `60` | Intervalo mínimo entre refrescos del JWKS, incluso con un `kid` desconocido |

### Modo local

Con `ENVIRONMENT=local`, el servicio acepta `X-Authenticated-Sub` sin un JWT (igual que
`bff-web`). Fuera de `local` ese modo no existe; lo verifica
`tests/unit/sesion/test_autenticacion.py::test_el_modo_local_no_existe_fuera_de_local`.
