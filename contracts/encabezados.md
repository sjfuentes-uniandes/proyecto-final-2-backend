# Encabezados entre servicios

Encabezados que los servicios internos envían y respetan, además de los de cada OpenAPI.

## `X-Deadline-Ms` (HU-W05)

Milisegundos que **quien llama seguirá esperando** la respuesta. Es un entero positivo y un tiempo **restante relativo**, no un instante absoluto.

**Por qué relativo y no `X-Request-Deadline` (instante absoluto).** Un instante absoluto exige que los relojes de las tareas de Fargate estén sincronizados al milisegundo. Con un presupuesto de 700 ms, un desfase de decenas de ms cambia el resultado. El restante relativo no depende del reloj de nadie. El salto de red solo puede acortarlo: quien llama corta con ese mismo valor, así que quien recibe nunca trabaja de más.

| Quién | Rol | Valor |
| --- | --- | --- |
| `api-socios` | Lo **inicia** en `POST /v1/cotizaciones` | min(`cotizacion_timeout_ms`, restante de `socio.cotizar`) |
| `bff-web` | Lo **inicia** en `POST /v1/cotizaciones` | min(`cotizacion_timeout_ms`, restante de `portal.cotizar`) |
| `cotizacion` | Lo **recibe** en `POST /v1/cotizaciones` | Presupuesto del recorrido = min(`latency_budget_ms.cotizacion`, recibido) |
| `cotizacion` | Lo **envía** a `catalogo` (producto y reglas vigentes) y a `clientes` (identidad y verificación del consentimiento) | El timeout efectivo de esa llamada: min(`dependencia_timeout_ms`, 700, restante − 20) |
| `cotizacion` | Lo **envía** a `adaptador-datos` (desde HU-W12) | `fuente_timeout_ms` − 10 |
| `catalogo` | Lo **respeta** en `GET /v1/productos/{productoId}` y `.../reglas-prima/vigente` | Corta en min(`consulta_timeout_ms`, 700, recibido − 20): **504 `TIEMPO_AGOTADO`** |
| `clientes` | Lo **respeta** en `GET /v1/clientes/{clienteId}/identidad` | Igual que `catalogo` |
| `adaptador-datos`, `adaptador-identidad` | Lo **respetan** desde HU-W12 y HU-M06 (máximo 700) | Igual, con el timeout del aliado (`ALLY_<ALIADO>_TIMEOUT_MS`) |

Reglas:

- Es **opcional**. Sin el encabezado, el servicio usa su propio presupuesto o su tope configurado.
- Un valor que no es un entero ≥ 1 se rechaza con `422 SOLICITUD_INVALIDA`.
- Quien recibe un **504** de una dependencia lo trata como **tiempo agotado**: no lo reintenta.
- El presupuesto de cada ambiente vive en `latency_budget_ms` de `infra/envs/<env>/apps.tfvars` y llega como `LATENCY_BUDGET_MS`. Ningún valor está fijo en el código, salvo el valor por omisión para local.

Implementación: `solventa_common.presupuesto` (presupuesto del recorrido y encabezado) y `solventa_common.resiliencia` (`timeout_efectivo_ms`, protecciones y `acotar`). Guía: [docs/hu-w05-limite-tiempo.md](../docs/hu-w05-limite-tiempo.md).
