"""N2 HU-W07: dos instancias de cotizacion (A y B) sobre el mismo PostgreSQL, el mismo Valkey
(CACHE_URL) y los mismos catalogo y clientes (arnés de conftest.py).

- (a) AC3: la misma Idempotency-Key, el mismo actor y el mismo cuerpo en B devuelven la cotización
  que creó A (201 en A, 200 en B, el mismo id), con una sola fila y sin recalcular en B.
- (b) AC3: `GET /v1/cotizaciones/{cotizacionId}` en B devuelve lo que respondió A, tras pasar por
  PostgreSQL (decimales, orden de coberturas y fechas).
- (c) La misma clave a la vez en A y en B: una sola fila, el mismo id, un 201 y un 200.
- (d) La misma clave con otro actor, desde B: otra cotización (la clave es por actor).
- (e) La misma clave y el mismo actor con otro cuerpo, desde B: IDEMPOTENCIA_CONFLICTO (422,
  contracts/codigos-error.md), sin fila nueva.

**Por qué las dos instancias van en el mismo proceso.** Son dos llamadas a `crear_app` de la
fábrica `cotizacion`, cada una con su TestClient (su bucle de eventos, su pool de PostgreSQL, su
cliente HTTP, su cliente de Valkey y sus cachés) y su exportador de spans. Lo justifica la N1
(tests/unit/test_hu_w07_sin_estado_local.py): en `src/cotizacion` el único estado de módulo es
`CONTEXTO` de `decimal`, que no cambia ningún resultado, y las cachés son de cada instancia.

**Lo que sí comparten en el proceso, y cómo se trata.** `solventa_common.telemetry` guarda el tracer
en un estado de módulo (`_estado`) que cada `crear_app` reemplaza: los spans de `operacion()` de las
dos instancias van al exportador de la última creada. Los del servidor HTTP, no: cada app tiene su
proveedor. Por eso cada solicitud lleva su propio X-Correlation-Id, que todos sus spans llevan como
atributo, y los spans se buscan en los dos exportadores. Que la atendió B se comprueba con el span
del servidor, que solo está en el exportador de B. En producción hay una app por proceso.
"""

import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

import httpx
import pytest
from solventa_common.correlation import ATRIBUTO_CORRELACION

from .conftest import ACTOR, Bases, sql

pytest.importorskip("docker")

# El otro actor de la suite (test_cotizacion_w03.py): la fixture solo define ACTOR.
SOCIO = {"X-Actor-Id": "SOC-000001", "X-Actor-Rol": "SOCIO"}
C01 = "00000000-0000-4000-8000-000000000001"
DATOS_C01 = {"fechaNacimiento": "1990-04-18", "plazoCreditoMeses": 180, "sumaAsegurada": 180000000}
IMAGEN_VALKEY = "valkey/valkey:8.0.2-alpine"
DEPENDENCIA = "cotizacion.dependencia."
CARRERAS = 5


def cuerpo(**cambios: Any) -> dict[str, Any]:
    return {
        "clienteId": C01,
        "productoId": "vida-hipotecario",
        "datosAsegurado": DATOS_C01,
        "coberturasOpcionales": ["ENFERMEDADES_GRAVES", "AUXILIO_FUNERARIO"],
        **cambios,
    }


def cotizar(
    app: Any, clave: str, datos: dict[str, Any] | None = None, actor: dict[str, str] = ACTOR
) -> tuple[httpx.Response, str]:
    """POST /v1/cotizaciones con su propio X-Correlation-Id, que devuelve para buscar sus spans."""
    correlacion = f"hu-w07-{uuid4()}"
    encabezados = {**actor, "Idempotency-Key": clave, "X-Correlation-Id": correlacion}
    respuesta = app.cliente.post("/v1/cotizaciones", json=datos or cuerpo(), headers=encabezados)
    return respuesta, correlacion


def filas_de_la_clave(bases: Bases, clave: str) -> list[tuple[Any, ...]]:
    return sql(
        bases.cotizacion,
        "SELECT id::text, actor_id FROM cotizaciones WHERE idempotency_key = %s",
        clave,
    )


def spans_de(correlacion: str, *apps: Any) -> list[Any]:
    """Los spans de una solicitud, en los exportadores de todas las instancias (ver docstring)."""
    return [
        span
        for app in apps
        for span in app.lista_spans()
        if (span.attributes or {}).get(ATRIBUTO_CORRELACION) == correlacion
    ]


def dependencias(correlacion: str, *apps: Any) -> list[str]:
    return sorted(s.name for s in spans_de(correlacion, *apps) if s.name.startswith(DEPENDENCIA))


def la_atendio(app: Any, correlacion: str) -> bool:
    """El span del servidor HTTP solo va al exportador de la instancia que atendió."""
    return any(
        s.kind.name == "SERVER" and (s.attributes or {}).get(ATRIBUTO_CORRELACION) == correlacion
        for s in app.lista_spans()
    )


def sin(cuerpo_json: dict[str, Any], *campos: str) -> dict[str, Any]:
    """El cuerpo sin los campos dados; `oferta.estado` quita el estado de la oferta."""
    copia = {k: v for k, v in cuerpo_json.items() if k not in campos}
    if "oferta.estado" in campos and copia.get("oferta"):
        copia["oferta"] = {k: v for k, v in copia["oferta"].items() if k != "estado"}
    return copia


# --- Valkey y catalogo con CACHE_URL ---------------------------------------------------------


@pytest.fixture(scope="module")
def valkey() -> Iterator[str]:
    import redis
    from testcontainers.core.container import DockerContainer

    with DockerContainer(IMAGEN_VALKEY).with_exposed_ports(6379) as contenedor:
        url = f"redis://{contenedor.get_container_host_ip()}:{contenedor.get_exposed_port(6379)}"
        limite = time.monotonic() + 30
        while True:
            try:
                if redis.Redis.from_url(url).ping():
                    break
            except redis.RedisError:
                if time.monotonic() > limite:
                    raise
                time.sleep(0.2)
        yield url


@pytest.fixture(scope="module")
def catalogo_con_cache(bases: Bases, valkey: str, servir_app: Any) -> Iterator[str]:
    """catalogo con CACHE_URL: llena Valkey, que A y B solo leen. Se calienta antes de empezar."""
    from catalogo.config import Settings as SettingsCatalogo
    from catalogo.main import crear_app as crear_catalogo

    # La primera operación abre la conexión: 50 ms (producción) no alcanzan en la CI.
    settings = SettingsCatalogo(
        SOLVENTA_DATABASE_URL=bases.catalogo, CACHE_URL=valkey, cache_timeout_ms=2000
    )
    with servir_app(crear_catalogo(settings)) as url:
        for ruta in ("", "/reglas-prima/vigente"):
            httpx.get(f"{url}/v1/productos/vida-hipotecario{ruta}", timeout=10).raise_for_status()
        yield url


@pytest.fixture
def replicas(
    cotizacion: Callable[..., Any], catalogo_con_cache: str, valkey: str
) -> tuple[Any, Any]:
    """A y B: dos apps de la fábrica, abiertas a la vez, con la misma configuración."""
    comun = {"CATALOGO_URL": catalogo_con_cache, "CACHE_URL": valkey, "cache_timeout_ms": 2000}
    a, b = cotizacion(**comun), cotizacion(**comun)
    assert a.cliente is not b.cliente and a.spans is not b.spans
    assert a.cliente.app is not b.cliente.app
    return a, b


# --- (a) y (b): AC3 --------------------------------------------------------------------------


def test_a_la_misma_clave_en_b_devuelve_la_de_a_sin_recalcular(replicas: tuple[Any, Any]) -> None:
    a, b = replicas
    clave = str(uuid4())
    creada, en_a = cotizar(a, clave)
    assert creada.status_code == 201, creada.text
    repetida, en_b = cotizar(b, clave)
    assert repetida.status_code == 200, repetida.text

    cuerpo_a, cuerpo_b = creada.json(), repetida.json()
    assert cuerpo_b["id"] == cuerpo_a["id"]
    assert cuerpo_a["pasos"] and cuerpo_b["pasos"] == []
    # A leyó el producto y las reglas de Valkey: las dos instancias comparten la caché.
    catalogo = next(p for p in cuerpo_a["pasos"] if p["codigo"] == "CATALOGO_Y_REGLAS")
    assert catalogo["origen"] == "CACHE"
    assert sin(cuerpo_b, "pasos", "oferta.estado") == sin(cuerpo_a, "pasos", "oferta.estado")

    assert filas_de_la_clave(a.bases, clave) == [(cuerpo_a["id"], ACTOR["X-Actor-Id"])]
    assert la_atendio(a, en_a) and not la_atendio(b, en_a)
    assert la_atendio(b, en_b) and not la_atendio(a, en_b)
    assert dependencias(en_a, a, b), "A calculó: sus dependencias deben verse"
    assert dependencias(en_b, a, b) == [], "B no recalcula"


def test_b_el_get_en_b_devuelve_lo_que_respondio_a(replicas: tuple[Any, Any]) -> None:
    a, b = replicas
    creada, _ = cotizar(a, str(uuid4()))
    assert creada.status_code == 201, creada.text
    cuerpo_a = creada.json()

    leida = b.cliente.get(f"/v1/cotizaciones/{cuerpo_a['id']}", headers=ACTOR)
    assert leida.status_code == 200, leida.text
    cuerpo_b = leida.json()
    assert cuerpo_b["pasos"] == []
    assert sin(cuerpo_b, "pasos", "oferta.estado") == sin(cuerpo_a, "pasos", "oferta.estado")
    # Lo que más fácil cambiaría al pasar por PostgreSQL, explícito.
    for campo in (
        "primaTotal",
        "desglose",
        "entradas",
        "coberturas",
        "calculadoEn",
        "vigenteHasta",
    ):
        assert cuerpo_b[campo] == cuerpo_a[campo], campo
    assert cuerpo_b["oferta"]["precio"] == cuerpo_a["oferta"]["precio"]
    assert cuerpo_b["oferta"]["coberturas"] == cuerpo_a["oferta"]["coberturas"]
    assert len(cuerpo_a["oferta"]["coberturas"]) > 1, "el orden solo se prueba con varias"


# --- (c) carrera -----------------------------------------------------------------------------


def test_c_la_misma_clave_a_la_vez_en_a_y_en_b(replicas: tuple[Any, Any]) -> None:
    a, b = replicas
    ganadas = {"A": 0, "B": 0}
    carreras_reales = 0
    for _ in range(CARRERAS):
        clave = str(uuid4())
        barrera = threading.Barrier(2)

        def enviar(app: Any, clave: str = clave, barrera: Any = barrera) -> Any:
            barrera.wait(timeout=10)
            return cotizar(app, clave)

        with ThreadPoolExecutor(max_workers=2) as hilos:
            futuro_a, futuro_b = hilos.submit(enviar, a), hilos.submit(enviar, b)
            (en_a, corr_a), (en_b, corr_b) = futuro_a.result(30), futuro_b.result(30)

        estados = {"A": en_a.status_code, "B": en_b.status_code}
        assert sorted(estados.values()) == [200, 201], (estados, en_a.text, en_b.text)
        assert en_a.json()["id"] == en_b.json()["id"]
        ((fila_id, _),) = filas_de_la_clave(a.bases, clave)
        assert fila_id == en_a.json()["id"]

        ganadora = "A" if estados["A"] == 201 else "B"
        ganadas[ganadora] += 1
        # La que perdió también calculó si consultó antes de que la otra guardara: hubo carrera.
        perdedora = corr_b if ganadora == "A" else corr_a
        if dependencias(perdedora, a, b):
            carreras_reales += 1
    print(
        f"\nHU-W07 (c): ganó A {ganadas['A']}, ganó B {ganadas['B']}; "
        f"carreras reales (las dos calcularon) {carreras_reales} de {CARRERAS}"
    )
    assert carreras_reales, "ninguna repetición llegó a competir: la prueba no probó la carrera"


# --- (d) y (e): alcance por actor y conflicto ------------------------------------------------


def test_d_la_misma_clave_con_otro_actor_es_otra_cotizacion(replicas: tuple[Any, Any]) -> None:
    a, b = replicas
    clave = str(uuid4())
    del_asesor, _ = cotizar(a, clave)
    del_socio, _ = cotizar(b, clave, actor=SOCIO)
    assert (del_asesor.status_code, del_socio.status_code) == (201, 201), del_socio.text
    assert del_socio.json()["id"] != del_asesor.json()["id"]
    filas = filas_de_la_clave(a.bases, clave)
    assert len(filas) == 2 and set(filas) == {
        (del_asesor.json()["id"], ACTOR["X-Actor-Id"]),
        (del_socio.json()["id"], SOCIO["X-Actor-Id"]),
    }


def test_e_la_misma_clave_con_otro_cuerpo_en_b_es_conflicto(replicas: tuple[Any, Any]) -> None:
    a, b = replicas
    clave = str(uuid4())
    creada, _ = cotizar(a, clave)
    assert creada.status_code == 201, creada.text
    otra, _ = cotizar(b, clave, cuerpo(coberturasOpcionales=["AUXILIO_FUNERARIO"]))
    assert (otra.status_code, otra.json()["codigo"]) == (422, "IDEMPOTENCIA_CONFLICTO")
    assert set(otra.json()) == {"codigo", "mensaje", "correlationId", "detalles"}
    assert filas_de_la_clave(a.bases, clave) == [(creada.json()["id"], ACTOR["X-Actor-Id"])]
