# Contratos

Contratos que publica el backend para sus consumidores (web, móvil, socios y otros servicios).

## Convención

- **Generados desde el código.** Cada OpenAPI de `openapi/<servicio>.yaml` se genera con `app.openapi()` y una prueba de contrato (`services/<servicio>/tests/contract/test_openapi.py`) verifica que el archivo publicado sea igual a lo que sirve la app. Los esquemas de eventos y del modelo de riesgo se generan desde Pydantic (`TypeAdapter(...).json_schema()`) y también se verifican con una prueba.
- **Excepción:** `openapi/clientes.propuesta-cotizacion.yaml` se escribe a mano. Es la propuesta del consumidor (`cotizacion`) mientras no exista el servicio `clientes`; hay que ratificarla con quienes lo construyan.
- Un cambio de contrato se hace primero aquí; el frontend lo copia con `scripts/sync-contracts.sh <ref>` desde un *commit* de `main`.

## Archivos

| Archivo | Contenido | Dueño | Cómo se regenera |
| --- | --- | --- | --- |
| `openapi/bff-web.yaml` | API de `bff-web` | Por confirmar (responsable de `bff-web`) | Ver [Regenerar un contrato](#regenerar-un-contrato) |
| `openapi/api-socios.yaml` | API de `api-socios`: `/admin/socios` (solo `bff-web`; alta, plan y consumo) y rutas del socio (HU-W01, HU-W02) | Integrante 1 (`api-socios`) | Ver [Regenerar un contrato](#regenerar-un-contrato) |
| `pact/bff-web-api-socios.json` | Pacto consumidor `bff-web` → proveedor `api-socios` (HU-W01, HU-W02) | Integrante 1 | `make test-contract SERVICE=bff-web` lo genera; `make test-contract SERVICE=api-socios` lo verifica |
| `openapi/bff-movil.yaml` | API de `bff-movil` para la app móvil: sesión, verificación de identidad y registro (HU-M06); `/v1/consentimientos` (otorgar y listar), `/v1/consentimientos/{consentimientoId}` y `/v1/consentimientos/texto-vigente` (HU-M08); `/v1/consentimientos/{consentimientoId}/revocacion` y `.../historial` (HU-M09). El titular sale siempre del token | Por confirmar (responsable de `bff-movil`) | Ver [Regenerar un contrato](#regenerar-un-contrato) |
| `pact/bff-movil-clientes.json` | Pacto consumidor `bff-movil` → proveedor `clientes`: rutas de consentimiento (HU-M08) y su revocación e historial (HU-M09). Se coteja con `openapi/clientes.yaml`; la verificación del lado de `clientes` está pendiente | Por confirmar (responsable de `bff-movil`) | `make test-contract SERVICE=bff-movil` lo genera |
| `openapi/adaptador-datos.yaml` | API `/v1/consultas` de `adaptador-datos` (HU-W10) | @capbmaster | Ver [Regenerar un contrato](#regenerar-un-contrato) |
| `openapi/adaptador-identidad.yaml` | API `/v1/verificaciones` de `adaptador-identidad`: verifica la selfie y la prueba de vida con el proveedor KYC (HU-M06) | Por confirmar (responsable de `adaptador-identidad`) | Ver [Regenerar un contrato](#regenerar-un-contrato) |
| `openapi/clientes.yaml` | API de `clientes`: `/v1/clientes/actual`, `/v1/clientes/{clienteId}/identidad` y `/v1/clientes/actual/identidad/verificaciones` (HU-M06); `/v1/clientes/actual/consentimientos` (otorgar y listar), `/v1/clientes/actual/consentimientos/{consentimientoId}` y `/v1/consentimientos/textos/vigente` (HU-M08); `.../{consentimientoId}/revocacion` y `.../{consentimientoId}/historial` (HU-M09). Cumple la identidad de `clientes.propuesta-cotizacion.yaml` | Por confirmar (responsable de `clientes`) | Ver [Regenerar un contrato](#regenerar-un-contrato) |
| `events/ConsentimientoOtorgado.v1.schema.json` | Evento `ConsentimientoOtorgado` v1 del outbox de `clientes` (HU-M08); `aggregateVersion` es opcional y vale 1 | Por confirmar (responsable de `clientes`) | `uv run python -m clientes.adapters.outbound.eventos` (desde la raíz) |
| `events/ConsentimientoRevocado.v1.schema.json` | Evento `ConsentimientoRevocado` v1 del outbox de `clientes` (HU-M09); `aggregateVersion` es opcional y vale 2 | Por confirmar (responsable de `clientes`) | `uv run python -m clientes.adapters.outbound.eventos` (desde la raíz) |
| `openapi/clientes.propuesta-cotizacion.yaml` | Contrato propuesto `cotizacion` → `clientes` (identidad y consentimiento) | @capbmaster; lo ratifican quienes construyan `clientes` (HU-W30/W31) | A mano. `npx @redocly/cli lint` |
| `alcances-perfilamiento.json` | Propósito, alcances (fuentes), tipo, textos es-CO / en-US y campos canónicos | @capbmaster | A mano |
| `datos-sinteticos/clientes-perfilamiento.json` | Clientes sintéticos C01–C19: identidad, consentimiento, datos declarados, comportamiento del simulador y resultado esperado | @capbmaster | A mano. Después de cambiarlo, regenerar los mapeos del simulador: `python services/simulador-aliados/scripts/generar_mapeos.py todo` |
| `codigos-error.md` | Códigos de error estables | Todos | A mano |
| `operaciones.md` | Nombres de operaciones medidas | Todos | A mano |

## Regenerar un contrato

Si cambia la API de un servicio, regenerar su archivo desde la raíz del repositorio. La prueba `tests/contract/test_openapi.py` del servicio falla mientras el archivo publicado no coincida con lo que sirve la app.

```bash
uv run python -c "import yaml; from adaptador_datos.main import app; open('contracts/openapi/adaptador-datos.yaml', 'w').write(yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True))"
```

```bash
uv run python -c "import yaml; from adaptador_identidad.main import app; open('contracts/openapi/adaptador-identidad.yaml', 'w').write(yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True))"
```

```bash
uv run python -c "import yaml; from clientes.main import app; open('contracts/openapi/clientes.yaml', 'w').write(yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True))"
```

```bash
uv run python -c "import yaml; from bff_web.main import app; open('contracts/openapi/bff-web.yaml', 'w').write(yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True))"
```

```bash
uv run python -c "import yaml; from bff_movil.main import app; open('contracts/openapi/bff-movil.yaml', 'w', encoding='utf-8').write(yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True))"
```

```bash
uv run python -c "import yaml; from api_socios.main import app; open('contracts/openapi/api-socios.yaml', 'w').write(yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True))"
```

Después de cambiar `adaptador-datos.yaml`, avisar a `cotizacion` (usa `AdaptadorDatosHttp`). Después de cambiar `bff-web.yaml`, sincronizar el frontend con `scripts/sync-contracts.sh <ref>`.

## Datos sintéticos

Todos los datos son **100 % sintéticos**: Solventa no usa datos reales de clientes ni proveedores reales. El simulador sustituye a Open Finance y a Datos Abiertos, y así las pruebas, los logs y las trazas nunca exponen información personal o financiera de nadie. Los identificadores de perfilamiento siguen el patrón `00000000-0000-4000-8000-0000000000NN`; los `consentimientoId` del stub, `00000000-0000-4000-9000-0000000000NN`.

En `simulador.<FUENTE>.datos`, una clave con punto (`listasRestrictivas.coincidencias`) es una ruta anidada; con el comportamiento `incompleta`, las claves listadas se **eliminan** de la respuesta nominal.
