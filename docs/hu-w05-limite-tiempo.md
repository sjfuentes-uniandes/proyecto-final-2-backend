# HU-W05 · Límite de tiempo por dependencia

> Como socio de distribución quisiera que Solventa limite el tiempo de espera de una fuente externa para evitar que una dependencia lenta bloquee la cotización (issue #46).

## 1. Alcance y decisiones

- **Reutiliza la resiliencia común.** `solventa_common.resiliencia` (`timeout_efectivo_ms`, `SinProteccion`, `ProteccionDependencia` y `con_reintento`) ya existía desde HU-W12. HU-W05 le agrega dos cosas:
  - El registro del timeout y los atributos de presupuesto en el span (`anotar_llamada` y `registrar_tiempo_agotado`).
  - `acotar`, para quien recibe un deadline.

  El presupuesto del recorrido vive en un módulo nuevo y pequeño, `solventa_common.presupuesto`. No se duplica ninguna lógica de reintento, circuito ni bulkhead.
- **Recorrido de cotización = `POST /v1/cotizaciones`.** Llama en paralelo a `catalogo` (producto y reglas), a `clientes` (identidad) y a su base (perfil). **No llama a `adaptador-datos`**: el perfil se calcula antes (perfilamiento) y la cotización solo lo lee. No se agregó esa llamada, para no cambiar los diagramas (`modelo_componentes.puml`: `PerfilCotizacion → ClientesIdentidad (2)` y `→ CatalogoReglas (3)`). Por eso el N3 usa la fuente lenta del simulador en el **perfil** y después cotiza con ese perfil (decisión acordada antes de implementar).
- **Encabezado `X-Deadline-Ms` (restante relativo).** Se descartó `X-Request-Deadline` (instante absoluto): exige relojes sincronizados al milisegundo entre tareas de Fargate, y con un presupuesto de 700 ms un desfase de decenas de ms cambia el resultado. El relativo no depende de ningún reloj. Ya era la convención de `adaptador-datos` y `adaptador-identidad` (HU-W12, HU-M06). Contrato: [contracts/encabezados.md](../contracts/encabezados.md).
- **Dos presupuestos distintos en `cotizacion`.**
  - `cotizacion_presupuesto_ms` (250) sigue siendo la **meta p95** que se informa al portal (C03/C04 de Figma, `latency_targets`).
  - `latency_budget_ms.cotizacion` (700 en `int`) es el **corte** del recorrido.

  Usar 250 como corte haría que el servicio no arrancara en `int` (`SOLVENTA_DEPENDENCIA_TIMEOUT_MS` = 300, DEF-03).
- **`api-socios` y `bff-web` inician el encabezado** con su propio presupuesto (`socio.cotizar` y `portal.cotizar` en `latency_budget_ms`). `clientes` también lo respeta, porque está en el recorrido.
- **Sin SSM.** Los tiempos no son sensibles y ya llegan como variables de entorno desde `apps.tfvars`. SSM agregaría IAM y una lectura al arrancar sin ningún beneficio.
- **Sin *fallback* (HU-W06).** Queda el punto de extensión `PoliticaDependencias` (sección 4).

## 2. Presupuesto del recorrido y propagación

```text
api-socios / bff-web ── X-Deadline-Ms = min(timeout, restante de socio.cotizar|portal.cotizar) ──▶ cotizacion
cotizacion: presupuesto = min(LATENCY_BUDGET_MS["cotizacion"], X-Deadline-Ms recibido)
  ├─ catalogo.producto / catalogo.reglas_prima ─ X-Deadline-Ms = min(dependencia_timeout_ms, 700, restante − 20) ─▶ catalogo
  ├─ clientes.identidad ─────────────────────── ídem ─▶ clientes
  └─ perfil (base propia) ───────────────────── SinProteccion con el restante
catalogo / clientes: corte en timeout_efectivo_ms(consulta_timeout_ms, X-Deadline-Ms) → 504 TIEMPO_AGOTADO
```

**En `cotizacion`:**
- `CalcularPrima.ejecutar` abre `presupuesto.iniciar(...)` dentro de `operacion("cotizacion")`.
- `ProductosCatalogo` y `ClientesHttp` llaman con `llamar_protegido` (`adapters/outbound/http_protegido.py`):
  - Usan `ProteccionDependencia`: reintento acotado de 1 intento a los 25 ms, solo ante conexión o 5xx que no sea 504, y solo si quedan ≥ 100 ms. Más el circuito por recurso y el bulkhead `catalogo` o `clientes`.
  - Toman el restante como `deadline_ms` y envían su timeout efectivo en `X-Deadline-Ms`.
  - Un 504 de la dependencia es tiempo agotado: no se reintenta.
- La verificación de consentimiento es un `POST`, así que solo lleva timeout, sin reintento.
- `_medir` es la red de seguridad: min(timeout + 10 ms, restante). Cubre la caché, el candado y un puerto sin protección.

**Validación al arrancar.** Si un timeout de dependencia no es **menor** que su presupuesto, `Settings()` falla y el servicio no arranca. Aplica así:
- `cotizacion`: `dependencia_timeout_ms` contra `cotizacion`.
- `api-socios`: `cotizacion_timeout_ms` contra `socio.cotizar`.
- `bff-web`: `cotizacion_timeout_ms` contra `portal.cotizar`.

El error no imprime la configuración (`hide_input_in_errors`; se probó con `DB_PASSWORD`).

## 3. Registro del timeout (AC1)

- Log `WARNING` **"dependencia agotó su tiempo"** con `proveedor`, `operacion`, `timeout_ms` y `restante_ms`. El `correlation_id`, el `trace_id` y el `span_id` los agrega el formateador de `solventa_common`.
- Atributos del span por dependencia:
  - `solventa.timeout_ms`: el timeout efectivo.
  - `solventa.restante_ms`: el deadline que recibió la llamada.
  - `solventa.presupuesto_ms`: el presupuesto del recorrido. También va en el span de `cotizacion`, en el de `socio.cotizar` y en el de `consulta.<FUENTE>` de `adaptador-datos`.

  En X-Ray quedan como metadatos del segmento; `correlation_id` y `operation` siguen siendo anotaciones.
- Resultados de las operaciones:
  - La operación de la dependencia queda `timeout`.
  - Si el recorrido falla por esa causa, `cotizacion` también queda `timeout`.

  Las dos cuentan en `OperationErrors`. Ver [contracts/operaciones.md](../contracts/operaciones.md).

## 4. Resultado del recorrido y punto de extensión de HU-W06

- Una dependencia obligatoria que agota su tiempo responde **503 `DEPENDENCIA_NO_DISPONIBLE`** (`detalles.dependencia`), que es recuperable. En el dominio es `DependenciaAgotada`, una subclase de `DependenciaNoDisponible`: quien no la distingue la trata igual.
- `CalcularPrima(..., politica=PoliticaDependencias)` recibe cada falla técnica con un `ResultadoDependencia`, `TIEMPO_AGOTADO` o `NO_DISPONIBLE`. La política por omisión, `FallaCerrada`, lanza el 503.
- **Para el dueño de HU-W06:** implementa una política que, para las dependencias que admitan un sustituto, devuelva un valor en lugar de lanzar. Hoy el protocolo declara `NoReturn`; cambia el tipo de retorno y que `_consultar` use el sustituto. La prueba `test_la_politica_de_hu_w06_recibe_el_resultado_de_la_dependencia` muestra cómo se inyecta.
- **Resuelto en HU-W06:** ninguna dependencia del recorrido admite sustituto y `FallaCerrada` se mantiene. Lo degradable son las fuentes del perfil, con reglas versionadas de `catalogo`. Ver [hu-w06-fallback.md](hu-w06-fallback.md).

## 5. Configuración por ambiente y conexiones

| Valor | Dónde | `int` | Por omisión (local) |
| --- | --- | --- | --- |
| Presupuesto del recorrido | `latency_budget_ms.cotizacion` | 700 | 700 (`PRESUPUESTO_RECORRIDO_MS`) |
| Presupuesto del socio y del portal | `latency_budget_ms["socio.cotizar"]`, `["portal.cotizar"]` | 1600 | 2000 |
| Timeout hacia `catalogo` y `clientes` | `SOLVENTA_DEPENDENCIA_TIMEOUT_MS` (`service_environment`) | 300 (DEF-03) | 150 |
| Tope de lectura de `catalogo` y `clientes` | `SOLVENTA_CONSULTA_TIMEOUT_MS` | — | 500 |
| Timeout de los aliados | `ally_timeouts_ms` → `ALLY_<ALIADO>_TIMEOUT_MS` | 800 (open-finance, datos-abiertos) | 700 |

`LATENCY_BUDGET_MS` llega a `cotizacion`, `api-socios` y `bff-web` (`latency_budget_readers` en `infra/apps/iam.tf`). Ningún tiempo está fijo en el código, salvo el valor por omisión para local.

### Conexiones

`cotizacion` usa un solo `httpx.AsyncClient` para `catalogo`, `clientes` y `adaptador-datos`:
- **`max_connections` = 150** (`SOLVENTA_HTTP_MAX_CONEXIONES`). Cubre los dos bulkheads de 50 (`catalogo` y `clientes`) más las ramas del perfilamiento (4 por perfil). Así, ante saturación, rechaza el **bulkhead**, que no espera y responde 503 al instante, y no el pool, que haría esperar y consumiría el presupuesto. La configuración valida que `max_connections ≥ 2 × bulkhead_max`.
- **`max_keepalive_connections` = 40** (`SOLVENTA_HTTP_MAX_KEEPALIVE`). Con `task_cpu` 512 (0,5 vCPU, override de `int`), una tarea sostiene del orden de 50–100 cotizaciones/s. A ~50 ms por dependencia y 3 dependencias, eso son ≈ 15 conexiones activas en régimen. 40 deja holgura para ráfagas sin acumular sockets ociosos que la tarea pequeña no necesita.

**Liberación al cancelar.** `asyncio.wait_for` cancela la solicitud y `httpcore` cierra esa conexión: no vuelve al pool a medio leer. La prueba N2 `test_el_corte_libera_la_tarea_y_la_conexion` lo verifica contra WireMock con un pool de una sola conexión: no quedan tareas (`asyncio.all_tasks`) ni conexiones ocupadas, y la siguiente solicitud obtiene la conexión sin esperar.

**Limitación.** `clientes` lee en `asyncio.to_thread`: su 504 sale a tiempo, pero el hilo termina la consulta.

## 6. Figma

Ninguna pantalla nueva para HU-W05. La relación con el diseño:
- **C03 "Calculando":** presupuesto total y porción por fuente.
- **C07 "Fuente degradada":** presupuesto asignado, tiempo agotado, reintentos y circuito. Es la pantalla de HU-W06.
- **C09:** tarifa base si Solventa no responde a tiempo, en el canal del socio.

Diferencias con el backend, que manda:
- El Figma muestra 2 reintentos con espera creciente; la regla común es 1 reintento.
- El Figma muestra 120 ms por fuente; en `int` son 600 ms, por DEF-03.
- El "presupuesto total de 250 ms" del Figma es la meta p95, no el corte.

## 7. Pruebas y resultados (ejecutados el 2026-10-24, en local)

| Nivel | Qué cubre | Resultado |
| --- | --- | --- |
| N1 `solventa-common` | Presupuesto (propio y recibido, por tarea, nunca negativo), encabezado, `LATENCY_BUDGET_MS`, validación (timeout ≥ presupuesto), timeout efectivo con y sin deadline, **cancelación real medida** (150 ms + tolerancia de 60 ms, sin tareas pendientes), `acotar`, reintento solo ante 5xx o conexión y solo con presupuesto, *backoff* con presupuesto agotado, AC2 con dos dependencias, WARNING con correlación y atributos del span (`tests/unit/test_hu_w05_presupuesto.py`) | **33 passed**; `presupuesto.py` y `resiliencia.py` al **100 %** de líneas y ramas |
| N1 `cotizacion` | Arranque con `LATENCY_BUDGET_MS` y falla con timeout ≥ presupuesto (sin filtrar `DB_PASSWORD`), AC1, AC3, deadline recibido y propio, perfil lento, política de HU-W06, propagación a `catalogo` (restante − margen, 5xx con reintento, 504 sin reintento, lento) y a `clientes` (GET con reintento, POST sin él), ruta con `X-Deadline-Ms` (503 a tiempo, 422 si es inválido) (`tests/unit/cotizacion/test_hu_w05_limite_tiempo.py`) | `make test-cov SERVICE=cotizacion`: **603 passed**; líneas **98,91 %**, ramas **96,77 %** |
| N1 `catalogo`, `clientes` | 504 a tiempo con deadline, tope sin deadline, deadline holgado normal, 422 si es inválido | `make test-cov`: `catalogo` **237** (99,45 % / 98,17 %), `clientes` **432** (99,93 % / 99,44 %) |
| N1 `api-socios`, `bff-web` | Envían el restante de su presupuesto, el timeout lo acota, abandonan a tiempo, validación al arrancar | `api-socios` **182** (99,30 % / 97,83 %), `bff-web` **271** (98,32 % / 92,17 %) |
| N2 `adaptador-datos` (WireMock 3.9.2 real) | Nominal C01 (AC3), lento C06 abandonado en 280 ms con WARNING y traza (AC1), caído C07 con un reintento dentro de 400 ms y sin reintento con 140 ms (AC2), liberación de tarea y conexión (`tests/module/test_hu_w05_limite_tiempo_wiremock.py`) | `make test-module SERVICE=adaptador-datos`: **19 passed** (5 de W05 y las de W10 y W12); estables en 4 corridas. Salida: [evidencias/hu-w05/suite-adaptador-datos.txt](evidencias/hu-w05/suite-adaptador-datos.txt) |
| N2 regresión | `cotizacion` (W03, W04, W10, W11, W12, W13, W31 y centinelas) y `catalogo` | **94** y **23 passed** |
| Traza del fallo controlado | Export de los spans (exportador en memoria) y del WARNING: C06 contra WireMock y `cotizacion` con `catalogo` lento | [evidencias/hu-w05/traza-fallo-controlado.json](evidencias/hu-w05/traza-fallo-controlado.json) |
| Contrato | OpenAPI regenerados (`cotizacion`, `catalogo`, `clientes`: encabezado opcional y 504); pactos y Schemathesis | `make test-contract`: `cotizacion` **25**, `catalogo` **18**, `clientes` **89**, `api-socios` **38**, `bff-web` **24**, `adaptador-datos` **4** |
| N3 R1 | `tests/e2e/test_hu_w05_r1_limite_tiempo.py` contra `int` | **No ejecutado**: la plataforma está destruida. Sin ambiente se omite con el motivo (3 skipped) |
| Regresión | `make test-unit` (todo el monorepo); `make test-module` de `clientes`, `api-socios`, `bff-web` y `libs` | **2646 passed**; módulo **135**, **7**, **17** y **8 passed**. Lint y `mypy` limpios (en `api-socios`, los 2 `unused-ignore` de `dependencias.py` que ya están en `main`) |
| Infraestructura | `terraform fmt`, `validate` y `terraform test` de `infra/apps` (`sprint2` ahora exige `LATENCY_BUDGET_MS` en `cotizacion`, `api-socios` y `bff-web` con las tres claves) | Válido; **8 passed** |

**Notas de las corridas:**
- **Testcontainers en esta máquina:** necesita `TESTCONTAINERS_RYUK_DISABLED=true`, porque Docker Desktop rechaza montar `docker.sock` para Ryuk.
- **`test_ut_w12_11` (W12):** separa las ramas por 10 ms, y bajo cobertura una recolección gen2 (31–41 ms, medida) caía en esa ventana. Ahora llama a `gc.collect()` antes de medir; no cambia lo que verifica.

## 8. AC → evidencia

| Criterio | Evidencia |
| --- | --- |
| **AC1** Si un proveedor supera su timeout, Solventa abandona la llamada dentro del límite configurado y registra el timeout con el `correlationId` | N1 `test_la_llamada_lenta_se_abandona_dentro_del_limite`, `test_el_timeout_se_registra_con_proveedor_operacion_y_correlacion`, `test_ac1_dependencia_lenta_se_abandona_en_su_timeout`; N2 `test_ac1_lento_se_abandona_dentro_del_limite_y_se_registra` (504 en 280–360 ms, WARNING con `correlation_id`, span con `result=timeout` y atributos); export de la traza; N3 `test_hu_w05_r1_la_fuente_lenta_se_abandona_y_el_perfil_llega_a_tiempo` y `..._la_traza_muestra_la_dependencia_agotada` (pendientes de ambiente) |
| **AC2** Con varias dependencias, si una consume su tiempo máximo, los reintentos no exceden el presupuesto del recorrido | N1 `test_backoff_y_presupuesto_del_recorrido_con_varias_dependencias`, `test_presupuesto_agotado_no_reintenta`, `test_5xx_reintenta_si_queda_presupuesto` (el reintento usa la misma ventana), `test_catalogo_5xx_reintenta_una_vez_con_la_proteccion`; N2 `test_ac2_caido_reintenta_una_vez_dentro_del_presupuesto` y `test_ac2_caido_sin_presupuesto_para_reintentar` |
| **AC3** Un proveedor que responde antes del timeout se usa normalmente | N1 `test_ac3_dependencias_a_tiempo_se_usan_normalmente`, `test_acotar_devuelve_el_resultado_a_tiempo`; N2 `test_ac3_nominal_responde_a_tiempo`; N3 `test_hu_w05_r1_la_cotizacion_llega_dentro_del_presupuesto` (pendiente) |
| Regla: timeout configurable por dependencia, menor que el presupuesto total | `apps.tfvars` (sección 5); validación al arrancar en `cotizacion`, `api-socios` y `bff-web` (`test_un_timeout_que_no_es_menor_que_el_presupuesto_no_arranca` y equivalentes) |
| Regla: el timeout libera recursos y se registra con proveedor, operación y correlación | `test_el_corte_libera_la_tarea_y_la_conexion` (N2), `test_la_llamada_lenta_se_abandona_dentro_del_limite` (sin tareas pendientes); WARNING (sección 3) |
| Regla: los reintentos solo aplican a errores recuperables y respetan el presupuesto restante | `es_conexion_o_5xx` (sin timeouts ni 4xx); el 504 no se reintenta (`test_catalogo_504_es_tiempo_agotado_y_no_se_reintenta`); el POST no se reintenta; AC2 |
| Propagación del encabezado | `test_en_el_recorrido_envia_el_restante_menos_el_margen`, `test_la_ruta_usa_el_deadline_recibido_y_responde_503`, pruebas de `catalogo`, `clientes`, `api-socios` y `bff-web` |

## 9. Pendiente

- **Ejecutar en AWS (`int`)**: con la plataforma arriba, ejecutar en orden:
  1. `make infra-aplicaciones ENV=int`, para `latency_budget_readers` y `latency_budget_ms`.
  2. `make infra-microservicios SERVICES=cotizacion,catalogo,clientes,api-socios,bff-web,adaptador-datos ENV=int`.
  3. `uv run pytest tests/e2e -k hu_w05` (N3 R1). Requiere el usuario E2E del back-office y, para la traza, credenciales con `xray:GetTraceSummaries` y `xray:BatchGetTraces`.
- **Medir en `int`** el p95 de `cotizacion` con el corte de 700 ms y confirmar que las alarmas de HU-W28 no cambian.
- **HU-W06**: política degradada sobre `PoliticaDependencias` (sección 4) y las pantallas C07 y C09.
