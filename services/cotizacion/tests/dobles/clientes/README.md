# Doble de `clientes` para las pruebas de módulo de `cotizacion`

Sustituye al stub de `clientes` que tenía el simulador (retirado en la PR 3.6). Solo lo usan las
pruebas de módulo de `cotizacion` (`tests/module/conftest.py`): se carga en el WireMock de esas
pruebas por su API de administración (`POST /__admin/mappings`) y `cotizacion` lo llama en
`{wiremock}/clientes`.
Ningún entorno lo usa: el compose y la infraestructura apuntan a `clientes` real.

Los mapeos se escriben a mano y siguen los datos sintéticos de
`contracts/datos-sinteticos/clientes-perfilamiento.json`. `tests/contract/test_dobles_clientes.py`
(`make test-contract SERVICE=cotizacion`) comprueba que cada respuesta cumpla
`contracts/openapi/clientes.yaml` y los DTO de `ClientesHttp`, y que coincida con su caso. La PR 3.7
lleva las pruebas de módulo a `clientes` real.

| Solicitud | Respuesta |
| --- | --- |
| `GET /clientes/v1/clientes/{clienteId}/identidad` | `{"clienteId", "estado", "verificadoEn"}` del caso; `verificadoEn = 2026-10-01T00:00:00Z` si la identidad es `VERIFICADO` |
| `POST /clientes/v1/clientes/{clienteId}/consentimientos/verificaciones` con `proposito = PERFILAMIENTO_PRECIO` | `{"decision", "consentimientoId", "vigenteHasta", "alcancesPermitidos", "alcancesRechazados", "causa"}` del caso. `vigenteHasta = 2027-10-01T00:00:00Z` si la decisión no es `RECHAZADO` |
| Cualquier otro `clienteId` (prioridad 10) | 404 `{"codigo": "CLIENTE_NO_ENCONTRADO", …}` |

| Caso | `clienteId` | Identidad | Consentimiento |
| --- | --- | --- | --- |
| C01, C03–C07, C13–C15, C18, C19 | `…01`, `…03`–`…07`, `…0d`–`…0f`, `…12`, `…13` | VERIFICADO | PERMITIDO, 4 alcances |
| C02 | `…02` | VERIFICADO | PERMITIDO_PARCIAL, sin `FA_INGRESOS_AGREGADOS` |
| C08 / C09 / C10 | `…08` / `…09` / `…0a` | VERIFICADO | RECHAZADO: `REVOCADO` / `VENCIDO` / `SIN_CONSENTIMIENTO` |
| C11 | `…0b` | RECHAZADO | PERMITIDO, 4 |
| C12 | `…0c` | PENDIENTE | PERMITIDO, 4 |
| C16 | `…10` | VERIFICADO | PERMITIDO_PARCIAL, sin `FA_HISTORIAL_PAGOS_12M` |
| C17 | `…11` | VERIFICADO | PERMITIDO_PARCIAL, solo `DA_REGISTROS_PUBLICOS` |

`clienteId` completo: `00000000-0000-4000-8000-0000000000NN`.
