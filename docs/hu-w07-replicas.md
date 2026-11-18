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

**Limitación del análisis estático.** No detecta la mutación que hace una función que **recibe el objeto como argumento**: por ejemplo, `decimal` escribe las `flags` del `Context` que recibe. Por eso todo objeto de módulo que no sea inmutable tiene que estar en `PERMITIDOS`, con una justificación revisable, aunque nadie lo asigne. Además, **la N1a solo revisa `services/cotizacion/src`, no `libs/`**: el estado de proceso de `solventa_common` se describe en §3.

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

Las dos cachés son atributos de cada instancia, no de la clase ni del módulo: `catalogo_http.py:190-191` (`_cache` y `_candados`) y `productos_catalogo.py:341-342` (`_memoria` y `_candados`). La N1b lo comprueba con dos instancias de cada adaptador. La app crea una instancia de cada una por tarea al arrancar (`main.py:182-203`).

| Qué | Dónde se guarda | Cuánto dura | Prueba de lo que guarda y cuánto dura |
|---|---|---|---|
| Modelo de riesgo vigente | Memoria de la réplica (`catalogo_http.py:193-200`) | `modelo_cache_ttl_s`, 60 s (`config.py:40`, `main.py:197-203`) | `tests/unit/perfilamiento/test_catalogo_http.py:80` (acierto), `:90` (vence a los 60 s), `:105` (sin TTL no se guarda) y `:128` (una sola solicitud en frío) |
| Versión cerrada del modelo | Memoria de la réplica (`catalogo_http.py:202-205`) | Sin vencimiento: no cambia | `test_catalogo_http.py:115` |
| Producto y reglas de prima, **con `CACHE_URL`** (activo en INT y EXP: `infra/envs/int/platform.tfvars:35`, `infra/envs/exp/platform.tfvars:29`) | **Valkey, compartido**: lo llena `catalogo` y `cotizacion` solo lo lee. Si Valkey falla o no tiene la clave, se pide a `catalogo` por HTTP, **sin pasar por la memoria** (`productos_catalogo.py:406-415`) | El TTL que pone `catalogo` | `tests/unit/cotizacion/test_productos_catalogo.py:193` (lee y nunca escribe) y `:207` (un fallo de Valkey va a `catalogo` sin escribir) |
| Producto y reglas de prima, **sin `CACHE_URL`** | Memoria de la réplica (`productos_catalogo.py:418-427`; con TTL 0 no se guarda, `:416-417`) | `catalogo_cache_ttl_s`, 60 s (`config.py:50`, `main.py:182-190`) | `test_productos_catalogo.py:159` (vence a los 61 s) y `:179` (una sola solicitud en frío) |
| Reglas de fallback (HU-W06) | **Siempre** en la memoria de la réplica, con o sin `CACHE_URL` (`solo_memoria=True`, `productos_catalogo.py:383`) | El mismo TTL de 60 s, y solo mientras rigen | `tests/unit/cotizacion/test_hu_w06_fallback.py:697` |

**Ventana de divergencia.** Tras un cambio en `catalogo`, dos réplicas pueden usar durante **hasta 60 s** un modelo vigente distinto o reglas de fallback distintas. Pasa lo mismo con el producto y las reglas de prima, pero solo si no hay `CACHE_URL`. Las versiones cerradas del modelo no cambian. Con `CACHE_URL`, el producto y las reglas de prima son los mismos para todas las réplicas, porque salen de Valkey o de `catalogo`. No se agregan cachés nuevas en memoria.

## 3. Estado por tarea (aceptado)

Se crea en `ciclo` (`main.py:137-219`), una vez por tarea, y no comparte datos de negocio:

- el pool de PostgreSQL (`main.py:150-158`) y el cliente HTTP con su pool de conexiones (`:161-169`);
- `ProteccionDependencia` (`:172-174`), con el circuito por dependencia y el bulkhead por servicio. La historia acepta los circuitos y bulkheads por tarea;
- el cliente de Valkey con su circuito propio (`main.py:177-178`; `libs/solventa-common/src/solventa_common/cache.py:63-72`);
- el `task_id` de la tarea, leído una vez (`main.py:139-140`; §3.2).

### 3.1 Estado de proceso de `solventa_common.telemetry`

- **Qué es:** `_estado` (`libs/solventa-common/src/solventa_common/telemetry.py:85`) guarda el tracer, el `TracerProvider` y los instrumentos de métricas.
- **Cuándo cambia:** `configurar` lo reemplaza en cada `crear_app` (`telemetry.py:155-204`).
- **Qué lo usa:** `operacion()` (`telemetry.py:265`) crea sus spans con ese tracer. La instrumentación de `httpx` también es global del proceso.
- **Por qué no afecta a las réplicas:** en ECS hay una app por proceso.
- **Qué cambia en las pruebas con dos apps en un proceso (N2):** los spans de `operacion()` de las dos van al exportador de la última app creada. Los del servidor HTTP, no, porque cada app instrumenta el suyo con su proveedor. Por eso cada solicitud se identifica por su `X-Correlation-Id` y se busca en los dos exportadores (7.4).
- **No se cambia en W07:** `libs/` queda fuera de la historia.

### 3.2 `solventa.task_id`

Identifica en X-Ray qué réplica atendió cada cotización (AC1; plan §b, decisión 2):

- **De dónde sale:**
  - Fargate inyecta `ECS_CONTAINER_METADATA_URI_V4` en cada contenedor. `Settings` la lee con ese nombre exacto, sin el prefijo `SOLVENTA_` (`config.py:69-73`).
  - `ciclo` la lee una vez al arrancar, en todas las ramas (`main.py:139-140`). Llama a `leer_task_id` (`adapters/outbound/metadatos_ecs.py`), que hace `GET <uri>/task` y toma el último segmento de `TaskARN` (`arn:aws:ecs:<región>:<cuenta>:task/<clúster>/<id>`).
  - La lectura tiene un tope total de 1 s y no reintenta.
  - **No deja spans.** El GET va dentro de `suppress_instrumentation()` de OpenTelemetry, solo en `leer_task_id`. La instrumentación de `httpx` es global del proceso, y sin la supresión cada arranque dejaría en X-Ray una traza suelta: un span `GET` de cliente sin padre. La utilidad viene de `opentelemetry-instrumentation`, que ya llega por `solventa-common`.
  - El valor llega a `CalcularPrima` en `ConfigCotizacion.task_id`.
- **En qué span va:** en `cotizacion`, el de `operacion(OPERACION)`, con el atributo `solventa.task_id` (`calcular_prima.py:333-335`).
  - Va también en las repeticiones idempotentes y en los rechazos.
  - No va en los spans de las dependencias, ni en la respuesta, ni en los eventos, ni en los logs. No es PII: es el id de la tarea.
- **Sin metadatos:**
  - si falta la variable, no hay llamada;
  - si hay error de red, la respuesta no es 2xx, el JSON es inválido o no trae `TaskARN`, la app arranca igual y el span no lleva el atributo. Queda un aviso en el log con el tipo de error, sin la URI.
- **No se indexa en X-Ray:** no se agrega a `indexed_attributes` (`infra/apps/ecs.tf:31`). Basta verlo en la traza; buscar por él exigiría un cambio de infraestructura que AC1 no necesita.

## 4. Idempotencia

- **Vive en PostgreSQL.** Lo garantiza `UNIQUE (actor_id, idempotency_key)` en `cotizaciones` (`adapters/outbound/migraciones/003_cotizaciones.sql:24`, `:31`; restricción `cotizaciones_actor_id_idempotency_key_key`, `adapters/outbound/cotizaciones_pg.py:62`).
- **Su alcance es por actor:** la misma clave con otro actor es otra cotización.
- **Se consulta antes de calcular.** `_ejecutar` busca la existente antes de llamar a las dependencias (`application/cotizacion/calcular_prima.py:352-357`, `:494-498`). La misma clave con otro cuerpo es `ConflictoIdempotencia`.
- **Carrera entre dos solicitudes con la misma clave:** la unicidad hace perder a la segunda (`cotizaciones_pg.py:477-479`), que devuelve la cotización ganadora con `pasos` vacío (`calcular_prima.py:410-416`).
- **Valkey no participa en la idempotencia ni en la sesión:** solo sirve para leer el catálogo (§2).
- **Respuesta de la repetición (200):** `pasos` viene vacío. `oferta.estado` se calcula con el `ahora` de cada solicitud (`adapters/inbound/http/cotizaciones.py:398`) y puede pasar de vigente a vencida. El resto es igual: la N1c lo comprueba en memoria y la N2, contra PostgreSQL.

**Confirmado contra PostgreSQL** (N2, `tests/module/test_hu_w07_replicas.py`). Hay dos instancias, A y B, sobre el mismo PostgreSQL, el mismo Valkey y el mismo `catalogo`:
- **Repetición (a):** la misma clave, el mismo actor y el mismo cuerpo en B devuelven 200 con el `id` que creó A (201).
  - Comparé los JSON que devuelve FastAPI. Son iguales salvo `pasos` y `oferta.estado`; en la práctica `oferta.estado` tampoco cambia.
  - Hay una sola fila.
  - En las solicitudes de B no hay ningún span `cotizacion.dependencia.*`: B no recalcula.
- **GET en B (b):** devuelve lo mismo que la respuesta de A tras pasar por la base: `primaTotal`, `desglose`, `entradas`, el orden de las coberturas (también las de la oferta), `calculadoEn`, `vigenteHasta` y `oferta.precio`.
- **Carrera (c):** la misma clave, a la vez en A y en B, da una sola fila, el mismo `id`, un 201 y un 200.
  - En las corridas, las dos instancias calcularon (carrera real) 5 de 5 veces, y ganaron las dos.
  - La que pierde recibe la ganadora por la restricción de unicidad.
- **Alcance (d) y conflicto (e):** la misma clave con otro actor es otra cotización, en otra fila. Con otro cuerpo, B responde `IDEMPOTENCIA_CONFLICTO` (422) y no crea ninguna fila.

## 5. Riesgos y límites fuera del alcance de W07

- **`/health` consulta la base.** Hace `SELECT 1` en el pool y responde 503 si falla (`main.py:233-242`). ECS lo usa con `/app/healthcheck` (`infra/apps/ecs.tf:147`; `Dockerfile:16`). Una caída de RDS deja todas las tareas no saludables a la vez y ECS las reemplaza todas. Queda documentado y no se cambia en W07.
- **Fargate Spot en INT** (`infra/envs/int/apps.tfvars:10`; `infra/apps/modules/ecs-service/main.tf:59-62`) se mantiene (plan §b, decisión 3). Una interrupción de Spot también detiene una tarea. La N3 distinguirá su propio retiro de una interrupción por el `stoppedReason` de la tarea.
- **AC2** (retiro de una tarea no saludable) se cubre con la configuración existente y con la observación del reemplazo en la N3. No se provoca un fallo de salud real en INT (plan §b, decisión 9).

## 6. Pruebas

| Nivel | Prueba | Qué cubre |
|---|---|---|
| N1a | `tests/unit/test_hu_w07_sin_estado_local.py` (`test_n1a_*`) | Sin `global`/`nonlocal`; objetos de módulo solo en `PERMITIDOS`, cada uno justificado y existente; ninguna función muta un nombre de módulo; `CONTEXTO` y el contexto de `decimal` (§1) |
| N1b | El mismo archivo (`test_n1b_*`) | Las cachés de `CatalogoHttp` y `ProductosCatalogo` son de cada instancia: dos instancias no comparten lo guardado ni los candados (§2) |
| N1c | `tests/unit/cotizacion/test_hu_w07_serializacion.py` | Misma cotización, mismos pasos y mismo `ahora` dan los mismos bytes en `model_dump_json`, dos veces y con dos objetos construidos por separado; la repetición solo cambia `pasos` (§4) |
| N2 | `tests/module/test_hu_w07_replicas.py` (`test_a_*` a `test_e_*`) | Dos apps de la fábrica `cotizacion` en el mismo proceso (lo justifica la N1), sobre el mismo PostgreSQL, Valkey (`CACHE_URL`), `catalogo` y `clientes`: repetición sin recalcular, GET desde la otra instancia, carrera, alcance por actor y conflicto (§4). Los spans de `operacion()` van al exportador de la última app creada (`_estado` de `solventa_common.telemetry`), así que cada solicitud se identifica por su `X-Correlation-Id` |
| N1 (`task_id`) | `tests/unit/test_hu_w07_task_id.py` | Adaptador de metadatos con `respx`: el id, sin variable sin llamada, 5xx, JSON inválido, sin `TaskARN`, errores de red, tope de 1 s y ningún span de la lectura (con un control que sí lo deja). `Settings` con el nombre exacto. Span `cotizacion` con y sin `task_id` (también en la repetición y en el rechazo). Arranque con los metadatos en un puerto cerrado, en las dos ramas de `ciclo` (§3.2) |
| N3 | Paso 7.8 | Réplicas en INT |
