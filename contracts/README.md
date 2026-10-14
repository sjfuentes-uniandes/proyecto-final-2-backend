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
| `openapi/api-socios.yaml` | API de `api-socios`: `/admin/socios` (solo `bff-web`) y rutas del socio (HU-W01) | Integrante 1 (`api-socios`) | Ver [Regenerar un contrato](#regenerar-un-contrato) |
| `pact/bff-web-api-socios.json` | Pacto consumidor `bff-web` → proveedor `api-socios` (HU-W01) | Integrante 1 | `make test-contract SERVICE=bff-web` lo genera; `make test-contract SERVICE=api-socios` lo verifica |
| `openapi/adaptador-datos.yaml` | API `/v1/consultas` de `adaptador-datos` (HU-W10) | @capbmaster | Ver [Regenerar un contrato](#regenerar-un-contrato) |
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
uv run python -c "import yaml; from bff_web.main import app; open('contracts/openapi/bff-web.yaml', 'w').write(yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True))"
```

```bash
uv run python -c "import yaml; from api_socios.main import app; open('contracts/openapi/api-socios.yaml', 'w').write(yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True))"
```

Después de cambiar `adaptador-datos.yaml`, avisar a `cotizacion` (usa `AdaptadorDatosHttp`). Después de cambiar `bff-web.yaml`, sincronizar el frontend con `scripts/sync-contracts.sh <ref>`.

## Datos sintéticos

Todos los datos son **100 % sintéticos**: Solventa no usa datos reales de clientes ni proveedores reales. El simulador sustituye a Open Finance y a Datos Abiertos, y así las pruebas, los logs y las trazas nunca exponen información personal o financiera de nadie. Los identificadores de perfilamiento siguen el patrón `00000000-0000-4000-8000-0000000000NN`; los `consentimientoId` del stub, `00000000-0000-4000-9000-0000000000NN`.

En `simulador.<FUENTE>.datos`, una clave con punto (`listasRestrictivas.coincidencias`) es una ruta anidada; con el comportamiento `incompleta`, las claves listadas se **eliminan** de la respuesta nominal.
