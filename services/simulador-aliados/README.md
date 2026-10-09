# simulador-aliados

WireMock 3.9.2 con los simuladores de terceros (regla 9: nunca proveedores reales). Datos **100 % sintéticos**.

| Carpeta de mapeos | Qué simula | Origen |
| --- | --- | --- |
| `mappings/clientes-stub/` | Stub **temporal** de `clientes` (identidad y consentimiento) según la propuesta `contracts/openapi/clientes.propuesta-cotizacion.yaml` (DT-02). Se retira cuando existan HU-W30/W31 | Generado (02, CS-08) |
| `mappings/kyc/` | Proveedor de identidad | — |
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
