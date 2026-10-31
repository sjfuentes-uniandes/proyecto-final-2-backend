# simulador-aliados

WireMock 3.9.2 con los simuladores de terceros (nunca se usan proveedores reales). Datos **100 % sintéticos**.

| Carpeta de mapeos | Qué simula | Origen |
| --- | --- | --- |
| `mappings/open-finance/`, `__files/open-finance/` | Aliado Open Finance: productos vigentes, historial de pagos de 12 meses e ingresos agregados | Generado |
| `mappings/datos-abiertos/`, `__files/datos-abiertos/` | Aliado Datos Abiertos: registros públicos (listas restrictivas) | Generado |
| `mappings/kyc/` | Proveedor de identidad (selfie + prueba de vida), HU-M06 | A mano |
| `mappings/pagos/` | Pasarela de pagos (recaudo de la prima), HU-W16 | A mano |
| `mappings/firma/` | Firma electrónica de la póliza, HU-W17 | A mano |
| `mappings/push/` | Notificaciones push a dispositivos, HU-M11 | A mano |
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

## Pagos, firma y push (`/pagos`, `/firma`, `/push`), HU-S02

Los usa `adaptador-pagos` (`pagos`) y `adaptador-firma` (`firma` y `push`). Como en KYC, un valor sintético
del cuerpo elige el caso; cualquier otro valor cae en el respaldo de prioridad 10 (404). Todos exigen
`X-Api-Key` (cualquier valor) y `pagos` además `Idempotency-Key`. Los tiempos del caso lento superan el
timeout del aliado en `ally_timeouts_ms` (`infra/envs/int/apps.tfvars`).

| Aliado y solicitud | Campo | Nominal (200, 50 ms) | Lento (200) | Caído (503) | Malformado (200, JSON inválido) | Respaldo (404) |
| --- | --- | --- | --- | --- | --- | --- |
| `POST /pagos/v1/cobros` | `referencia` | `pago-sintetico-aprobado` → `{"estado": "APROBADO", "transaccionId": "TX-SINT-0001", "motivo": null}` | `pago-sintetico-lento`, 8000 ms (timeout 5 s) | `pago-sintetico-caido` | `pago-sintetico-malformado` | `referencia_no_reconocida` |
| `POST /firma/v1/firmas` | `documentoId` | `doc-sintetico-firmado` → `{"estado": "FIRMADO", "firmaId": "FIR-SINT-0001", "selloTiempo": "..."}` | `doc-sintetico-lento`, 8000 ms (timeout 5 s) | `doc-sintetico-caido` | `doc-sintetico-malformado` | `documento_no_reconocido` |
| `POST /push/v1/envios` | `tokenDispositivo` | `token-sintetico-entregado` → `{"estado": "ENVIADO", "envioId": "PUSH-SINT-0001"}` | `token-sintetico-lento`, 4000 ms (timeout 2 s) | `token-sintetico-caido` | `token-sintetico-malformado` | `token_no_reconocido` |

El caído responde `{"error": "unavailable"}`; el malformado, el cuerpo `{"estado": ` con `Content-Type: application/json`.

## Pagos por pagador sintético (`/pagos`), HU-W16

`adaptador-pagos` cobra el primer recaudo de la prima con `referencia` = `claveRecaudo` (uuid) y `pagador` = el
`clienteId` sintético de `OfertaAceptada` (ADR 0013, decisión 6: el proveedor emula una domiciliación previa; ningún
dato de tarjeta ni de cuenta). El caso lo elige el `pagador`, uno por cliente de
`contracts/datos-sinteticos/clientes-perfilamiento.json`. Los mapeos por `referencia` fija de la tabla anterior siguen
igual y no se solapan: estos exigen una `referencia` uuid.

**Cobro:** `POST /pagos/v1/cobros` con `X-Api-Key` e `Idempotency-Key` (cualquier valor) y el cuerpo
`{"referencia": "<uuid>", "monto": "64638", "moneda": "COP", "pagador": "<clienteId>"}`. La respuesta devuelve la
`referencia` recibida (plantilla de respuestas de WireMock, activa por mapeo con `"transformers": ["response-template"]`).

**Consulta de estado** (conciliación, P16-3): `GET /pagos/v1/cobros?referencia=<uuid>&pagador=<clienteId>` con
`X-Api-Key`. Responde el estado del cobro de ese pagador, sin memoria de los cobros hechos.

| Caso | Cliente (`pagador`) | Cobro | Consulta de estado |
| --- | --- | --- | --- |
| Aprobado | C01 `00000000-0000-4000-8000-000000000001` | 200 `{"referencia", "estado": "APROBADO", "transaccionId": "TX-SINT-C01", "motivo": null}`, 50 ms | 200, el mismo cuerpo |
| Rechazado | C02 `00000000-0000-4000-8000-000000000002` | 200 `{"referencia", "estado": "RECHAZADO", "transaccionId": null, "motivo": "FONDOS_INSUFICIENTES"}`, 50 ms | 200, el mismo cuerpo |
| Malformado | C05 `00000000-0000-4000-8000-000000000005` | 200 con el cuerpo `{"estado": ` (JSON inválido) | 404 `referencia_no_reconocida` (respaldo) |
| Lento (incierto) | C06 `00000000-0000-4000-8000-000000000006` | 200 `APROBADO` (`TX-SINT-C06`) en 8000 ms, por encima del timeout de 5 s | 200 `APROBADO` (`TX-SINT-C06`): el cobro sí ocurrió |
| Caído | C07 `00000000-0000-4000-8000-000000000007` | 503 `{"error": "unavailable"}` | 404 `{"error": "cobro_no_encontrado"}`: el cobro nunca llegó |
| Otro pagador, sin encabezados, `referencia` que no es uuid o sin `monto` ni `moneda` | — | 404 `referencia_no_reconocida` (respaldo) | 404 `referencia_no_reconocida` (respaldo) |

Los números de caso coinciden con el comportamiento de cada cliente en Open Finance y Datos Abiertos (C05 malformada,
C06 lenta y C07 caída). Solo C01 tiene un caso de suscripción aprobada en `cotizaciones-suscripcion.json` (S01);
los demás no tienen uno.

Pruebas de los mapeos contra WireMock 3.9.2 real, desde la raíz del repositorio. Necesitan Docker: sin él fallan, no se
omiten (también las incluye `pytest services/simulador-aliados/tests`):

```bash
uv run pytest services/simulador-aliados/tests/module
```

## Firma por cliente sintético (`/firma`), HU-W17

`adaptador-firma` firma la huella sha256 del PDF de la póliza (ADR 0014, decisión 3: se firma la huella, no el PDF).
El caso lo elige el `firmante` (el `clienteId` sintético), con los mismos clientes que pagos. Los mapeos por
`documentoId` fijo (`doc-sintetico-*`) de la tabla de HU-S02 siguen igual y no se solapan: estos exigen un
`documentoId` uuid y una huella bien formada.

**Firma:** `POST /firma/v1/firmas` con `X-Api-Key` e `Idempotency-Key` (cualquier valor) y el cuerpo
`{"documentoId": "<uuid>", "huella": "sha256:<64 hex en minúsculas>", "firmante": "<clienteId>"}`. La respuesta
devuelve el `documentoId` y la `huella` recibidos (plantilla de respuestas). `firmaId` y `selloTiempo` son fijos y
sintéticos: `FIR-SINT-C01` o `FIR-SINT-C06`, y `2026-01-01T00:00:00Z`. Sin rechazo, porque C02 nunca llega a
emitirse, y sin consulta de estado, porque reintentar con la misma `Idempotency-Key` es idempotente (ADR 0014).

| Caso | Cliente (`firmante`) | Respuesta |
| --- | --- | --- |
| Firmado | C01 `00000000-0000-4000-8000-000000000001` | 200 `{"documentoId", "estado": "FIRMADO", "firmaId": "FIR-SINT-C01", "selloTiempo": "2026-01-01T00:00:00Z", "huella"}`, 50 ms |
| Malformado | C05 `00000000-0000-4000-8000-000000000005` | 200 con el cuerpo `{"estado": ` (JSON inválido) |
| Lento (incierto) | C06 `00000000-0000-4000-8000-000000000006` | 200 `FIRMADO` (`FIR-SINT-C06`) en 8000 ms, por encima del timeout de 5 s: la firma sí ocurre |
| Caído | C07 `00000000-0000-4000-8000-000000000007` | 503 `{"error": "unavailable"}` |
| Otro firmante, sin encabezados, `documentoId` que no es uuid o huella mal formada | — | 404 `documento_no_reconocido` (respaldo) |

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
