# Operaciones medidas

Nombres de `Operation` en `OperationDuration` y `OperationErrors` (`solventa_common.telemetry.operacion`). El colector exporta `^Operation.*` con la dimensión `Operation`; no se crean métricas nuevas (DT-11).

Fuente: `docs/sprint_1/01-acuerdos-tecnicos.md` §3.10.

| Operación (`Operation`) | Servicio | Dónde | Resultados |
| --- | --- | --- | --- |
| `perfilamiento` | `cotizacion` | Caso de uso `crear_perfil` | `ok` (CALCULADO), `degradado` (DEGRADADO), `insuficiente` (INFORMACION_INSUFICIENTE), `rechazado` (4xx), `error` / `timeout` (5xx) |
| `perfilamiento.recalculo` | `cotizacion` | Caso de uso `recalcular_perfil` | Igual que `perfilamiento` |
| `perfilamiento.dependencia.clientes.identidad` | `cotizacion` | Llamada a la identidad del cliente | `ok`, `timeout`, `error` |
| `perfilamiento.dependencia.clientes.consentimiento` | `cotizacion` | Verificación del consentimiento | `ok`, `timeout`, `error` |
| `perfilamiento.dependencia.catalogo.modelo` | `cotizacion` | Consulta del modelo de riesgo | `ok`, `timeout`, `error` |
| `perfilamiento.dependencia.fuente.<FUENTE>` | `cotizacion` | Rama de cada fuente | `ok`, `timeout`, `error` |
| `consulta.<FUENTE>` | `adaptador-datos` | Caso de uso `consultar_fuente` | `ok`, `timeout`, `error`, `rechazado` |
| `modelo_riesgo.publicar`, `modelo_riesgo.consultar` | `catalogo` | Casos de uso del modelo | — |

`<FUENTE>` ∈ {`FA_PRODUCTOS_VIGENTES`, `FA_HISTORIAL_PAGOS_12M`, `FA_INGRESOS_AGREGADOS`, `DA_REGISTROS_PUBLICOS`}.

**Log de cierre del perfilamiento:** `logger.info("perfil calculado", extra={"perfil_id": …, "estado": …, "dependencias": [{"nombre": …, "ms": …, "resultado": …}]})`, dentro de la `operacion`. Las dependencias van en una **lista** para que `sanitizar` no redacte valores por el nombre de la clave.
