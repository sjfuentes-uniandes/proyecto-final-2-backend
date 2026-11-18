# HU-W07 · Réplicas independientes de la cotización

> Como responsable de producto quisiera que el procesamiento de cotizaciones pueda ejecutarse en réplicas independientes para aumentar capacidad sin modificar el flujo funcional.

Este documento es la auditoría de estado local de `cotizacion`. La amplían los pasos siguientes (N2, N3 y configuración de INT). Las rutas son relativas a `services/cotizacion/src/cotizacion/` salvo que se diga otra cosa.

## 1. Estado de módulo

`tests/unit/test_hu_w07_sin_estado_local.py` (N1a) recorre con `ast` todo `src/` y exige:

- **Ni `global` ni `nonlocal`.** Hoy no hay ninguno.
- **Ningún objeto de módulo mutable o construido fuera de `PERMITIDOS`.** Solo pasan sin estar en la lista los valores inmutables: constantes, tuplas, textos, alias de tipo, `frozenset`, `re.compile`, `Decimal`, `TypeVar` y `MappingProxyType`. La lista tiene 27 nombres, cada uno con su justificación:
  - **12 tablas constantes** de traducción: errores de OpenAPI, estado → resultado, motivo, severidad y comparadores.
  - **Objetos que se construyen una vez:**
    - los cuatro `APIRouter`, los tres `logger`, `settings` y `app`;
    - los textos SQL que arma `_insertar` y el `Traversable` de las migraciones;
    - `CONTEXTO` (§1.1).
  - Una prueba falla si un nombre de la lista deja de existir o ya es inmutable, para que la lista no quede obsoleta.
- **Ninguna función muta un nombre de módulo**, esté permitido o no: ni asignación por subíndice o atributo, ni `del`, ni `.append`, `.update`, `.setdefault`, `.pop` ni otros mutadores.
- No se cuenta el bloque `if __name__ == "__main__":` de `adapters/outbound/eventos.py:151-156`, que genera los esquemas de eventos y no corre al importar.

**Limitación del análisis estático.** No detecta la mutación que hace una función que **recibe el objeto como argumento**: por ejemplo, `decimal` escribe las `flags` del `Context` que recibe. Por eso todo objeto de módulo que no sea inmutable tiene que estar en `PERMITIDOS`, con una justificación revisable, aunque nadie lo asigne.

### 1.1 `CONTEXTO`: estado de módulo aceptado

- **Qué es:** `domain/cotizacion/prima.py:35` define `CONTEXTO = Context(prec=34, rounding=ROUND_HALF_EVEN)`.
- **Qué cambia:** `redondear` lo pasa a `scaleb` y a `quantize` (`prima.py:73-75`), y cada redondeo inexacto deja marcadas `Inexact` y `Rounded` en `CONTEXTO.flags`. Esas `flags` se acumulan en la memoria del proceso.
- **Por qué se acepta:** solo son señales informativas. Ningún resultado depende de ellas, porque los traps (`InvalidOperation`, `DivisionByZero`, `Overflow`) se evalúan en cada operación y no por las `flags` acumuladas. Nadie las lee ni las limpia.
- **Los `with localcontext(CONTEXTO):`** de `prima.py:96` y `domain/cotizacion/oferta.py:45` activan una **copia** de `CONTEXTO`, propia de cada tarea asyncio (`decimal.HAVE_CONTEXTVAR`), y no lo modifican.
- **Qué lo vigila:** `test_n1a_contexto_decimal_solo_con_localcontext_y_sin_tocar_sus_atributos` falla si en `src/`:
  - se lee o se escribe un atributo de `CONTEXTO` (`flags`, `traps`, `prec`, `rounding`, `Emin`, `Emax`, `capitals` o `clamp`) o se llama a `clear_flags` o a `clear_traps`;
  - aparecen `getcontext` o `setcontext`;
  - se usa `localcontext` con un argumento que no sea `CONTEXTO`.

## 2. Cachés por réplica (aceptadas; plan §b, decisión 1)

Las dos cachés son atributos de cada instancia, no de la clase ni del módulo: `catalogo_http.py:190-191` (`_cache` y `_candados`) y `productos_catalogo.py:341-342` (`_memoria` y `_candados`). La N1b lo comprueba con dos instancias de cada adaptador. La app crea una instancia de cada una por tarea al arrancar (`main.py:176-197`).

| Qué | Dónde se guarda | Cuánto dura | Prueba de lo que guarda y cuánto dura |
|---|---|---|---|
| Modelo de riesgo vigente | Memoria de la réplica (`catalogo_http.py:193-200`) | `modelo_cache_ttl_s`, 60 s (`config.py:40`, `main.py:191-197`) | `tests/unit/perfilamiento/test_catalogo_http.py:80` (acierto), `:90` (vence a los 60 s), `:105` (sin TTL no se guarda) y `:128` (una sola solicitud en frío) |
| Versión cerrada del modelo | Memoria de la réplica (`catalogo_http.py:202-205`) | Sin vencimiento: no cambia | `test_catalogo_http.py:115` |
| Producto y reglas de prima, **con `CACHE_URL`** (activo en INT y EXP: `infra/envs/int/platform.tfvars:35`, `infra/envs/exp/platform.tfvars:29`) | **Valkey, compartido**: lo llena `catalogo` y `cotizacion` solo lo lee. Si Valkey falla o no tiene la clave, se pide a `catalogo` por HTTP, **sin pasar por la memoria** (`productos_catalogo.py:406-415`) | El TTL que pone `catalogo` | `tests/unit/cotizacion/test_productos_catalogo.py:193` (lee y nunca escribe) y `:207` (un fallo de Valkey va a `catalogo` sin escribir) |
| Producto y reglas de prima, **sin `CACHE_URL`** | Memoria de la réplica (`productos_catalogo.py:418-427`; con TTL 0 no se guarda, `:416-417`) | `catalogo_cache_ttl_s`, 60 s (`config.py:50`, `main.py:176-184`) | `test_productos_catalogo.py:159` (vence a los 61 s) y `:179` (una sola solicitud en frío) |
| Reglas de fallback (HU-W06) | **Siempre** en la memoria de la réplica, con o sin `CACHE_URL` (`solo_memoria=True`, `productos_catalogo.py:383`) | El mismo TTL de 60 s, y solo mientras rigen | `tests/unit/cotizacion/test_hu_w06_fallback.py:697` |

**Ventana de divergencia.** Tras un cambio en `catalogo`, dos réplicas pueden usar durante **hasta 60 s** un modelo vigente distinto o reglas de fallback distintas. Pasa lo mismo con el producto y las reglas de prima, pero solo si no hay `CACHE_URL`. Las versiones cerradas del modelo no cambian. Con `CACHE_URL`, el producto y las reglas de prima son los mismos para todas las réplicas, porque salen de Valkey o de `catalogo`. No se agregan cachés nuevas en memoria.

## 3. Estado por tarea (aceptado)

Se crea en `ciclo` (`main.py:133-212`), una vez por tarea, y no comparte datos de negocio:

- el pool de PostgreSQL (`main.py:144-152`) y el cliente HTTP con su pool de conexiones (`:155-163`);
- `ProteccionDependencia` (`:166-168`), con el circuito por dependencia y el bulkhead por servicio. La historia acepta los circuitos y bulkheads por tarea;
- el cliente de Valkey con su circuito propio (`main.py:171-172`; `libs/solventa-common/src/solventa_common/cache.py:63-72`).

## 4. Idempotencia

- **Vive en PostgreSQL.** Lo garantiza `UNIQUE (actor_id, idempotency_key)` en `cotizaciones` (`adapters/outbound/migraciones/003_cotizaciones.sql:24`, `:31`; restricción `cotizaciones_actor_id_idempotency_key_key`, `adapters/outbound/cotizaciones_pg.py:62`).
- **Su alcance es por actor:** la misma clave con otro actor es otra cotización.
- **Se consulta antes de calcular.** `_ejecutar` busca la existente antes de llamar a las dependencias (`application/cotizacion/calcular_prima.py:345-350`, `:487-491`). La misma clave con otro cuerpo es `ConflictoIdempotencia`.
- **Carrera entre dos solicitudes con la misma clave:** la unicidad hace perder a la segunda (`cotizaciones_pg.py:477-479`), que devuelve la cotización ganadora con `pasos` vacío (`calcular_prima.py:403-409`).
- **Valkey no participa en la idempotencia ni en la sesión:** solo sirve para leer el catálogo (§2).
- **Respuesta de la repetición (200):** `pasos` viene vacío. `oferta.estado` se calcula con el `ahora` de cada solicitud (`adapters/inbound/http/cotizaciones.py:398`) y puede pasar de vigente a vencida. El resto debe ser igual; la N1c lo comprueba en memoria, y la N2 lo comprobará contra PostgreSQL.

## 5. Riesgos y límites fuera del alcance de W07

- **`/health` consulta la base.** Hace `SELECT 1` en el pool y responde 503 si falla (`main.py:226-235`). ECS lo usa con `/app/healthcheck` (`infra/apps/ecs.tf:147`; `Dockerfile:16`). Una caída de RDS deja todas las tareas no saludables a la vez y ECS las reemplaza todas. Queda documentado y no se cambia en W07.
- **Fargate Spot en INT** (`infra/envs/int/apps.tfvars:10`; `infra/apps/modules/ecs-service/main.tf:59-62`) se mantiene (plan §b, decisión 3). Una interrupción de Spot también detiene una tarea. La N3 distinguirá su propio retiro de una interrupción por el `stoppedReason` de la tarea.
- **AC2** (retiro de una tarea no saludable) se cubre con la configuración existente y con la observación del reemplazo en la N3. No se provoca un fallo de salud real en INT (plan §b, decisión 9).

## 6. Pruebas

| Nivel | Prueba | Qué cubre |
|---|---|---|
| N1a | `tests/unit/test_hu_w07_sin_estado_local.py` (`test_n1a_*`) | Sin `global`/`nonlocal`; objetos de módulo solo en `PERMITIDOS`, cada uno justificado y existente; ninguna función muta un nombre de módulo; `CONTEXTO` y el contexto de `decimal` (§1) |
| N1b | El mismo archivo (`test_n1b_*`) | Las cachés de `CatalogoHttp` y `ProductosCatalogo` son de cada instancia: dos instancias no comparten lo guardado ni los candados (§2) |
| N1c | `tests/unit/cotizacion/test_hu_w07_serializacion.py` | Misma cotización, mismos pasos y mismo `ahora` dan los mismos bytes en `model_dump_json`, dos veces y con dos objetos construidos por separado; la repetición solo cambia `pasos` (§4) |
| N2 | Paso 7.4 | Dos instancias sobre el mismo PostgreSQL y Valkey |
| N3 | Paso 7.8 | Réplicas en INT |
