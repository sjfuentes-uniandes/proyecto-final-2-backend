# simulador-aliados

WireMock 3.9.2 con los simuladores de terceros (nunca se usan proveedores reales). Datos **100 % sintéticos**.

| Carpeta de mapeos | Qué simula | Origen |
| --- | --- | --- |
| `mappings/clientes-stub/` | Stub **temporal** de `clientes` (identidad y consentimiento) según la propuesta `contracts/openapi/clientes.propuesta-cotizacion.yaml` . Se retira cuando exista el servicio `clientes` | Generado |
| `mappings/open-finance/`, `__files/open-finance/` | Aliado Open Finance: productos vigentes, historial de pagos de 12 meses e ingresos agregados | Generado |
| `mappings/datos-abiertos/`, `__files/datos-abiertos/` | Aliado Datos Abiertos: registros públicos (listas restrictivas) | Generado |
| `mappings/kyc/` | Proveedor de identidad (selfie + prueba de vida), HU-M06 | A mano |
| `mappings/health.json` | `GET /health` para el *healthcheck* | A mano |

## Generación

Los mapeos generados salen de `contracts/datos-sinteticos/clientes-perfilamiento.json`. **No se editan a mano**: se cambia el archivo de datos y se regeneran desde la raíz del repositorio:

```bash
python services/simulador-aliados/scripts/generar_mapeos.py todo
```

La prueba verifica que lo versionado sea idéntico a lo generado:

```bash
uv run --with pytest pytest services/simulador-aliados/tests
```

## Stub de `clientes` (`/clientes-stub`)

| Solicitud | Respuesta |
| --- | --- |
| `GET /clientes-stub/v1/clientes/{clienteId}/identidad` | `{"clienteId", "estado", "verificadoEn"}` del caso |
| `POST /clientes-stub/v1/clientes/{clienteId}/consentimientos/verificaciones` con `proposito = PERFILAMIENTO_PRECIO` | `{"decision", "consentimientoId", "vigenteHasta", "alcancesPermitidos", "alcancesRechazados", "causa"}` del caso. `vigenteHasta = 2027-10-01T00:00:00Z` si la decisión no es `RECHAZADO` |
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

## Proveedor KYC (`/kyc`), HU-M06

El proveedor real de selfie + prueba de vida no existe: el valor sintético que manda `adaptador-identidad`
en el campo `selfie` selecciona el caso (nunca una imagen real). Cualquier otro valor cae en el
respaldo de prioridad 10: 404 `{"error": "selfie_no_reconocida"}`.

| Solicitud (`POST /kyc/v1/verificaciones`) | `selfie` | Respuesta |
| --- | --- | --- |
| Aprobada | `selfie-sintetica-aprobada` | 200 `{"resultado": "APROBADO", "motivo": null}` |
| Captura inválida (AC2) | `selfie-sintetica-captura-invalida` | 200 `{"resultado": "CAPTURA_INVALIDA", "motivo": "ILUMINACION_INSUFICIENTE"}` |
| Rostro no coincide | `selfie-sintetica-rechazada` | 200 `{"resultado": "RECHAZADO", "motivo": "ROSTRO_NO_COINCIDE"}` |
| Proveedor caído (AC3) | `selfie-sintetica-caida` | 503 `{"error": "unavailable"}` |
| Proveedor lento (AC3, supera el timeout de 3 s del proveedor) | `selfie-sintetica-lenta` | 200 con `fixedDelayMilliseconds: 5000` |

Exige `X-Api-Key` (cualquier valor), igual que los demás aliados.

## Aliados (`/open-finance` y `/datos-abiertos`)

Cada mapeo exige `X-Api-Key` (cualquier valor) y el parámetro de campos **exacto** de la fuente. Una solicitud que no coincide (otro cliente, otros campos o sin `X-Api-Key`) cae en el respaldo de prioridad 10: 404 `{"error":"customer_not_found"}`.

| Fuente | Solicitud |
| --- | --- |
| `FA_PRODUCTOS_VIGENTES` | `GET /open-finance/v3/customers/{id}/active-products?fields=creditorCount,oldestProductYears,totalMonthlyInstallment` |
| `FA_HISTORIAL_PAGOS_12M` | `GET /open-finance/v3/customers/{id}/payment-history?months=12&fields=lateCount` |
| `FA_INGRESOS_AGREGADOS` | `GET /open-finance/v3/customers/{id}/aggregated-income?fields=estimatedMonthlyIncome` |
| `DA_REGISTROS_PUBLICOS` | `GET /datos-abiertos/v1/registros/{id}?campos=listasRestrictivas` |

| Comportamiento | Respuesta |
| --- | --- |
| `nominal` | 200 nominal en 50 ms |
| `demora150` | 200 nominal en 150 ms |
| `lento` | 200 nominal en 1000 ms |
| `caido` | 503 `{"error":"unavailable"}` en 50 ms |
| `incompleta` | 200 sin las claves indicadas en `datos` |
| `adicional` | 200 nominal con `accountNumbers`, `fullName` y `transactions` dentro de `data` |
| `malformada` | 200 con el contenedor (`data` / `registro`) igual a `"no-es-objeto"` |
| `escenario_recuperacion` | Escenario `recuperacion-<id>`: 1000 ms la primera vez y nominal (50 ms) después. Se reinicia con `POST /__admin/scenarios/reset` |

| Caso | Productos | Historial | Ingresos | Datos Abiertos |
| --- | --- | --- | --- | --- |
| C01 | nominal | nominal | nominal | nominal |
| C02 | nominal | nominal | — (no autorizado) | nominal |
| C03 | nominal | incompleta (sin `lateCount`) | nominal | nominal |
| C04 | adicional | nominal | nominal | nominal |
| C05 | malformada | nominal | nominal | nominal |
| C06 | nominal | nominal | nominal | lento |
| C07 | nominal | nominal | caído | nominal |
| C13 | nominal | lento | nominal | nominal |
| C14 | nominal | nominal | nominal | 1 coincidencia en listas |
| C15 | nominal | 1 mora | nominal | nominal |
| C18 | demora150 | demora150 | demora150 | demora150 |
| C19 | nominal | nominal | nominal | escenario de recuperación |
| C08–C12, C16, C17 | — | — | — | — (sin llamadas: 404 si se invocan) |
