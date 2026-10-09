# Contratos

Contratos que publica el backend para sus consumidores (web, móvil, socios y otros servicios).

## Convención (DT-12)

- **Generados desde el código.** Cada OpenAPI de `openapi/<servicio>.yaml` se genera con `app.openapi()` y una prueba de contrato (`services/<servicio>/tests/contract/test_openapi.py`) verifica que el archivo publicado sea igual a lo que sirve la app. Los esquemas de eventos y del modelo de riesgo se generan desde Pydantic (`TypeAdapter(...).json_schema()`) y también se verifican con una prueba.
- **Excepción:** `openapi/clientes.propuesta-cotizacion.yaml` se escribe a mano. Es la propuesta del consumidor (`cotizacion`) mientras no existan HU-W30/W31 (PD-01).
- Un cambio de contrato se hace primero aquí; el frontend lo copia con `scripts/sync-contracts.sh <ref>` desde un *commit* de `main`.

## Archivos

| Archivo | Contenido | Dueño | Cómo se regenera |
| --- | --- | --- | --- |
| `openapi/bff-web.yaml` | API de `bff-web` | Integrantes 1 y 4 | Ver la *docstring* de `services/bff-web/tests/contract/test_openapi.py` |
| `openapi/adaptador-datos.yaml` | API `/v1/consultas` de `adaptador-datos` (HU-W10) | Integrante 4 | Ver la *docstring* de `services/adaptador-datos/tests/contract/test_openapi.py` |
| `openapi/clientes.propuesta-cotizacion.yaml` | Contrato propuesto `cotizacion` → `clientes` (identidad y consentimiento) | Integrante 4; ratifican los dueños de W30/W31 | A mano. `npx @redocly/cli lint` |
| `alcances-perfilamiento.json` | Propósito, alcances (fuentes), tipo, textos es-CO / en-US y campos canónicos | Integrante 4 | A mano (01 §3.4) |
| `datos-sinteticos/clientes-perfilamiento.json` | Clientes sintéticos C01–C19: identidad, consentimiento, datos declarados, comportamiento del simulador y resultado esperado | Integrante 4 | A mano (01 §3.5). Después de cambiarlo, regenerar los mapeos del simulador: `python services/simulador-aliados/scripts/generar_mapeos.py todo` |
| `codigos-error.md` | Códigos de error estables | Todos | A mano |
| `operaciones.md` | Nombres de operaciones medidas | Todos | A mano |

## Datos sintéticos

Todos los datos son **100 % sintéticos** (regla 15). Los identificadores de perfilamiento siguen el patrón `00000000-0000-4000-8000-0000000000NN`; los `consentimientoId` del stub, `00000000-0000-4000-9000-0000000000NN`.

En `simulador.<FUENTE>.datos`, una clave con punto (`listasRestrictivas.coincidencias`) es una ruta anidada; con el comportamiento `incompleta`, las claves listadas se **eliminan** de la respuesta nominal.
