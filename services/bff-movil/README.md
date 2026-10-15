# bff-movil

BFF de la aplicación móvil. Compone las respuestas de los servicios de dominio para el canal
`/movil` (`infra/platform/edge.tf`); no reimplementa reglas de negocio.

## Módulos

| Módulo | Historia | Qué hace |
| --- | --- | --- |
| `sesion` | HU-M07 | `GET /v1/sesion`: revalida el JWT del pool `clientes` de Cognito y devuelve los servicios autorizados del cliente según su identidad |

## `GET /v1/sesion` (HU-M07)

Contrato: [`contracts/openapi/bff-movil.yaml`](../../contracts/openapi/bff-movil.yaml).

1. Revalida el `Authorization: Bearer <token>` contra el JWKS del emisor (`JWT_ISSUERS`):
   firma, `exp`, `token_use == "access"`, que `sub` sea igual a `X-Authenticated-Sub` y que el
   emisor sea el pool `clientes`. Un token de otro pool (por ejemplo, `backoffice`) recibe 403
   `ROL_NO_AUTORIZADO`; cualquier otro rechazo es 401 `NO_AUTENTICADO`.
2. Resuelve el `clienteId` del titular del token (`sub`) con un `ResolutorCliente`.
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

### Resolución de `clienteId` (pendiente, otra historia)

El repositorio no define todavía cómo se relaciona el `sub` de Cognito con el `clienteId` de
`clientes`. `adapters/outbound/resolutor_cliente_memoria.py` es un adaptador temporal con una
relación fija, marcado `TODO`. Se reemplaza cuando exista esa relación (probablemente un
atributo del perfil en `clientes`, poblado durante el registro o la verificación) — trabajo de
otra historia, no de HU-M07.

## Configuración

Además de lo común (`solventa_common.settings.ServiceSettings`):

| Variable | Alias sin prefijo | Por defecto | Qué es |
| --- | --- | --- | --- |
| `SOLVENTA_CLIENTES_URL` | `CLIENTES_URL` | `http://localhost:8083` | Base URL de `clientes` |
| `SOLVENTA_JWT_ISSUERS` | `JWT_ISSUERS` | `{}` | Emisor (`iss`) por pool de Cognito, JSON `{"customers": "...", "backoffice": "..."}` |
| `SOLVENTA_CLIENTES_TIMEOUT_MS` | — | `500` | Timeout de la llamada a `clientes` (tope real 700 ms, `solventa_common.resiliencia`) |
| `SOLVENTA_JWKS_TTL_S` | — | `3600` | Vigencia de las claves del emisor en caché |
| `SOLVENTA_JWKS_TIMEOUT_MS` | — | `500` | Timeout de la consulta al JWKS |
| `SOLVENTA_JWKS_REFRESCO_MIN_S` | — | `60` | Intervalo mínimo entre refrescos del JWKS, incluso con un `kid` desconocido |

### Modo local

Con `ENVIRONMENT=local`, el servicio acepta `X-Authenticated-Sub` sin un JWT (igual que
`bff-web`). Fuera de `local` ese modo no existe; lo verifica
`tests/unit/sesion/test_autenticacion.py::test_el_modo_local_no_existe_fuera_de_local`.
