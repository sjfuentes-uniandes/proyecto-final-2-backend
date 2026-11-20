"""Configuración y ciclo de vida del consumidor de `SiniestroAprobado` y de la conciliación de
indemnizaciones (HU-W23; ADR 0015, decisiones 3 y 5).

La infraestructura entrega `SQS_DINERO_INDEMNIZACIONES_URL` (`consumes`). Mismo criterio que las
tareas de HU-W16:

- Consumidor activo si llega la cola, con el mismo lote (2) y la misma espera larga (10 s).
- Conciliación encendida por defecto, sobre el pool y el mismo cliente del adaptador.
- Al cerrar, todas las tareas se señalizan a la vez y se esperan en paralelo; después el relay y al
  final el pool.
- Sin cola, base o cliente, o si una tarea falla, se registra y la app sigue sirviendo.
"""

import asyncio
import time
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any, ClassVar

import psycopg_pool
import pytest
from dinero import main
from dinero.adapters.outbound import indemnizaciones_pg, recaudos_pg
from dinero.config import VISIBILIDAD_INDEMNIZACIONES_S, Settings
from fastapi import FastAPI
from fastapi.testclient import TestClient
from solventa_common import inbox

COLA = "https://sqs.us-east-1.amazonaws.com/000000000000/solventa-int-dinero-indemnizaciones"
_NOMBRES = (
    "SQS_DINERO_INDEMNIZACIONES_URL",
    "SQS_DINERO_ACEPTACIONES_URL",
    "INDEMNIZACION_CONCILIACION_HABILITADA",
    "INDEMNIZACION_CONCILIACION_INTERVALO_S",
    "INDEMNIZACION_CONCILIACION_ANTIGUEDAD_MIN_S",
    "INDEMNIZACION_CONCILIACION_LOTE",
    "INDEMNIZACION_CONCILIACION_RECLAMO_S",
    "INDEMNIZACION_CONCILIACION_ALERTA_TRAS_S",
)


@pytest.fixture(autouse=True)
def sin_entorno(monkeypatch: pytest.MonkeyPatch) -> None:
    for nombre in _NOMBRES:
        monkeypatch.delenv(nombre, raising=False)
        monkeypatch.delenv(f"SOLVENTA_{nombre}", raising=False)


# --- Configuración ---------------------------------------------------------------------------------
def test_por_omision() -> None:
    settings = Settings()
    assert settings.sqs_dinero_indemnizaciones_url is None
    assert settings.indemnizacion_conciliacion_habilitada is True
    assert settings.indemnizacion_conciliacion_intervalo_s == 120
    assert settings.indemnizacion_conciliacion_antiguedad_min_s == 300
    assert settings.indemnizacion_conciliacion_lote == 10
    assert settings.indemnizacion_conciliacion_reclamo_s == 60
    assert settings.indemnizacion_conciliacion_alerta_tras_s == 3600


def test_la_cola_llega_como_la_pone_ecs_y_como_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SQS_DINERO_INDEMNIZACIONES_URL", COLA)
    assert Settings().sqs_dinero_indemnizaciones_url == COLA
    monkeypatch.delenv("SQS_DINERO_INDEMNIZACIONES_URL")
    monkeypatch.setenv("SOLVENTA_SQS_DINERO_INDEMNIZACIONES_URL", COLA + "-local")
    assert Settings().sqs_dinero_indemnizaciones_url == COLA + "-local"


def test_la_conciliacion_se_configura_con_solventa(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOLVENTA_INDEMNIZACION_CONCILIACION_HABILITADA", "false")
    monkeypatch.setenv("SOLVENTA_INDEMNIZACION_CONCILIACION_INTERVALO_S", "30")
    monkeypatch.setenv("SOLVENTA_INDEMNIZACION_CONCILIACION_ANTIGUEDAD_MIN_S", "600")
    monkeypatch.setenv("SOLVENTA_INDEMNIZACION_CONCILIACION_LOTE", "5")
    monkeypatch.setenv("SOLVENTA_INDEMNIZACION_CONCILIACION_RECLAMO_S", "90")
    monkeypatch.setenv("SOLVENTA_INDEMNIZACION_CONCILIACION_ALERTA_TRAS_S", "7200")
    settings = Settings()
    assert settings.indemnizacion_conciliacion_habilitada is False
    assert settings.indemnizacion_conciliacion_intervalo_s == 30
    assert settings.indemnizacion_conciliacion_antiguedad_min_s == 600
    assert settings.indemnizacion_conciliacion_lote == 5
    assert settings.indemnizacion_conciliacion_reclamo_s == 90
    assert settings.indemnizacion_conciliacion_alerta_tras_s == 7200


def test_la_antiguedad_minima_supera_la_continuacion_y_la_visibilidad_de_la_cola() -> None:
    """Una indemnización recién creada la paga su continuación (hasta 6,5 s) y su mensaje se puede
    reentregar tras la visibilidad (60 s): la conciliación no compite con ellos."""
    settings = Settings()
    assert VISIBILIDAD_INDEMNIZACIONES_S == 60
    assert settings.indemnizacion_conciliacion_antiguedad_min_s > 4 * VISIBILIDAD_INDEMNIZACIONES_S
    with pytest.raises(ValueError):
        Settings(indemnizacion_conciliacion_antiguedad_min_s=2 * VISIBILIDAD_INDEMNIZACIONES_S - 1)
    minima = Settings(indemnizacion_conciliacion_antiguedad_min_s=120)
    assert minima.indemnizacion_conciliacion_antiguedad_min_s == 120


def test_el_reclamo_cubre_el_peor_caso_de_un_movimiento() -> None:
    """Consulta más reintento del desembolso: 2 × el timeout de dinero (6,5 s)."""
    settings = Settings()
    peor_caso_s = 2 * settings.adaptador_pagos_timeout_ms / 1000
    assert settings.indemnizacion_conciliacion_reclamo_s > peor_caso_s


@pytest.mark.parametrize(
    "campo",
    [
        {"indemnizacion_conciliacion_intervalo_s": 0},
        {"indemnizacion_conciliacion_lote": 0},
        {"indemnizacion_conciliacion_lote": 51},
        {"indemnizacion_conciliacion_reclamo_s": 14},
        {"indemnizacion_conciliacion_alerta_tras_s": 0},
    ],
)
def test_limites_de_la_configuracion(campo: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        Settings(**campo)


# --- Dobles ----------------------------------------------------------------------------------------
class Bitacora:
    eventos: ClassVar[list[str]] = []


class PoolFalso:
    def __init__(self, url: str, **argumentos: Any) -> None:
        self.abierto = False

    async def open(self) -> None:
        self.abierto = True

    async def close(self) -> None:
        Bitacora.eventos.append("pool cerrado")
        self.abierto = False


class Tarea:
    comportamiento: ClassVar[str] = "espera"
    nombre: ClassVar[str] = ""

    def __init__(self, *argumentos: Any, **opciones: Any) -> None:
        self.argumentos = argumentos
        self.opciones = opciones
        type(self).creados.append(self)  # type: ignore[attr-defined]

    async def ejecutar(self, parada: asyncio.Event) -> None:
        if type(self).comportamiento == "falla":
            raise RuntimeError("fallo inesperado con datos sensibles")
        if type(self).comportamiento == "ignora_parada":
            await asyncio.sleep(3600)
        await parada.wait()
        Bitacora.eventos.append(f"{type(self).nombre} parado")


class ConsumidorFalso(Tarea):
    creados: ClassVar[list["ConsumidorFalso"]] = []
    nombre = "consumidor"


class ConciliacionFalsa(Tarea):
    creados: ClassVar[list["ConciliacionFalsa"]] = []
    nombre = "conciliacion"


class _Inerte(Tarea):
    """La conciliación de recaudos (HU-W16): aquí no interesa."""

    creados: ClassVar[list["_Inerte"]] = []
    nombre = "recaudos"


def _cliente_sqs_falso(read_timeout: float) -> Any:
    return SimpleNamespace(meta=SimpleNamespace(config=SimpleNamespace(read_timeout=read_timeout)))


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    llamadas: dict[str, list[Any]] = {"sqs": []}

    def sqs(region: str, *, endpoint_url: str | None = None, espera_larga_s: int) -> Any:
        llamadas["sqs"].append((region, endpoint_url, espera_larga_s))
        return _cliente_sqs_falso(espera_larga_s + inbox.MARGEN_LECTURA_S)

    monkeypatch.setattr(main, "cliente_sqs", sqs)
    return llamadas


@pytest.fixture
def tareas(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    Bitacora.eventos = []
    for clase in (ConsumidorFalso, ConciliacionFalsa):
        clase.creados = []
        clase.comportamiento = "espera"
    monkeypatch.setattr(main, "ConsumidorInbox", ConsumidorFalso)
    monkeypatch.setattr(main, "ConciliarIndemnizaciones", ConciliacionFalsa)
    monkeypatch.setattr(main, "ConciliarRecaudos", _Inerte)
    yield


@pytest.fixture
def crear(
    monkeypatch: pytest.MonkeyPatch, capturar_logs: Callable[[], Any], tareas: None
) -> Callable[..., tuple[FastAPI, Any]]:
    monkeypatch.setattr(recaudos_pg, "migrar", lambda url: None)
    monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", PoolFalso)

    def _crear(con_base: bool = True, **cambios: Any) -> tuple[FastAPI, Any]:
        settings = Settings(DB_HOST="rds.interno" if con_base else None, **cambios)
        app = main.crear_app(settings)
        return app, capturar_logs()

    return _crear


def _esperar(condicion: Callable[[], bool], limite_s: float = 5) -> None:
    fin = time.monotonic() + limite_s
    while not condicion():
        assert time.monotonic() < fin, "la condición no se cumplió a tiempo"
        time.sleep(0.01)


def _consumidores_de_indemnizaciones() -> list[ConsumidorFalso]:
    return [
        c
        for c in ConsumidorFalso.creados
        if c.opciones.get("reloj") is not None and c.argumentos[0]._cola_url == COLA
    ]


# --- Consumidor ------------------------------------------------------------------------------------
def test_sin_cola_no_hay_consumidor_y_se_avisa(
    crear: Callable[..., tuple[FastAPI, Any]], aws: dict[str, list[Any]]
) -> None:
    app, logs = crear()
    with TestClient(app) as cliente:
        assert cliente.get("/health").json() == {"status": "ok"}
        assert getattr(app.state, "consumidor_indemnizaciones", None) is None
    assert ConsumidorFalso.creados == []
    [aviso] = [
        r for r in logs.de_nivel("WARNING") if r["message"] == main.MENSAJE_INDEMNIZACIONES_SIN_COLA
    ]
    assert aviso["missing"] == "SQS_DINERO_INDEMNIZACIONES_URL"


def test_con_cola_consume_con_lote_2_y_espera_10_y_se_detiene_antes_del_pool(
    crear: Callable[..., tuple[FastAPI, Any]], aws: dict[str, list[Any]]
) -> None:
    app, logs = crear(
        SQS_DINERO_INDEMNIZACIONES_URL=COLA,
        AWS_REGION="us-east-2",
        AWS_ENDPOINT_URL="http://localstack:4566",
    )
    with TestClient(app) as cliente:
        tarea = app.state.consumidor_indemnizaciones
        assert isinstance(tarea, asyncio.Task) and not tarea.done()
        assert cliente.get("/health").json() == {"status": "ok"}
        pool = app.state.pool
    [consumidor] = _consumidores_de_indemnizaciones()
    receptor, repositorio, manejador = consumidor.argumentos
    assert isinstance(receptor, inbox.ReceptorSqs)
    assert (receptor.max_mensajes, receptor.espera_larga_s) == (2, 10)
    assert aws["sqs"] == [("us-east-2", "http://localstack:4566", 10)]
    assert isinstance(repositorio, inbox.InboxPg) and repositorio._pool is pool
    assert callable(manejador)
    assert consumidor.opciones["reloj"]().utcoffset() is not None
    assert tarea.done() and not tarea.cancelled()
    assert Bitacora.eventos.index("consumidor parado") < Bitacora.eventos.index("pool cerrado")
    assert logs.de_nivel("ERROR") == []


def test_con_cola_y_sin_base_arranca_y_lo_registra(
    crear: Callable[..., tuple[FastAPI, Any]], aws: dict[str, list[Any]]
) -> None:
    app, logs = crear(con_base=False, SQS_DINERO_INDEMNIZACIONES_URL=COLA)
    with TestClient(app) as cliente:
        assert cliente.get("/health").json() == {"status": "ok"}
    assert _consumidores_de_indemnizaciones() == []
    [error] = logs.de_nivel("ERROR")
    assert (error["message"], error["missing"]) == (
        main.MENSAJE_INDEMNIZACIONES_SIN_CONFIGURACION,
        "base",
    )


def test_si_no_se_puede_crear_el_cliente_sqs_arranca_y_lo_registra(
    crear: Callable[..., tuple[FastAPI, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def sqs(region: str, *, endpoint_url: str | None = None, espera_larga_s: int) -> Any:
        raise ValueError(f"Invalid endpoint: {endpoint_url}")

    monkeypatch.setattr(main, "cliente_sqs", sqs)
    app, logs = crear(SQS_DINERO_INDEMNIZACIONES_URL=COLA, AWS_ENDPOINT_URL="no-url")
    with TestClient(app) as cliente:
        assert cliente.get("/health").json() == {"status": "ok"}
    [error] = logs.de_nivel("ERROR")
    assert (error["message"], error["error_type"]) == (
        main.MENSAJE_INDEMNIZACIONES_NO_ARRANCO,
        "ValueError",
    )
    assert "no-url" not in logs.texto()


def test_un_consumidor_que_falla_no_tumba_la_aplicacion(
    crear: Callable[..., tuple[FastAPI, Any]], aws: dict[str, list[Any]]
) -> None:
    ConsumidorFalso.comportamiento = "falla"
    app, logs = crear(SQS_DINERO_INDEMNIZACIONES_URL=COLA)
    with TestClient(app) as cliente:
        tarea = app.state.consumidor_indemnizaciones
        _esperar(lambda: tarea.done())
        assert cliente.get("/health").json() == {"status": "ok"}
    [error] = logs.de_nivel("ERROR")
    assert (error["message"], error["error_type"]) == (
        main.MENSAJE_INDEMNIZACIONES_DETENIDO,
        "RuntimeError",
    )
    assert "datos sensibles" not in logs.texto()


def test_la_parada_del_consumidor_espera_con_limite_y_luego_cancela(
    crear: Callable[..., tuple[FastAPI, Any]],
    aws: dict[str, list[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ConsumidorFalso.comportamiento = "ignora_parada"
    monkeypatch.setattr(main, "PARADA_CONSUMIDOR_S", 0.05)
    app, logs = crear(SQS_DINERO_INDEMNIZACIONES_URL=COLA)
    inicio = time.monotonic()
    with TestClient(app):
        tarea = app.state.consumidor_indemnizaciones
    assert time.monotonic() - inicio < 5
    assert tarea.cancelled()
    [aviso] = [
        r
        for r in logs.de_nivel("WARNING")
        if r["message"] == main.MENSAJE_INDEMNIZACIONES_SIN_PARAR
    ]
    assert aviso["limit_s"] == 0.05


# --- Conciliación ----------------------------------------------------------------------------------
def test_la_conciliacion_arranca_por_defecto_con_el_pool_y_el_cliente_del_adaptador(
    crear: Callable[..., tuple[FastAPI, Any]], aws: dict[str, list[Any]]
) -> None:
    app, logs = crear(
        ADAPTADOR_PAGOS_URL="http://adaptador-pagos:8080",
        indemnizacion_conciliacion_intervalo_s=30,
        indemnizacion_conciliacion_antiguedad_min_s=600,
        indemnizacion_conciliacion_lote=4,
        indemnizacion_conciliacion_reclamo_s=45,
        indemnizacion_conciliacion_alerta_tras_s=7200,
    )
    with TestClient(app) as cliente:
        tarea = app.state.conciliacion_indemnizaciones
        assert isinstance(tarea, asyncio.Task) and not tarea.done()
        assert cliente.get("/health").json() == {"status": "ok"}
        pool, pagos = app.state.pool, app.state.pagos
    [conciliacion] = ConciliacionFalsa.creados
    opciones = conciliacion.opciones
    assert isinstance(opciones["indemnizaciones"], indemnizaciones_pg.IndemnizacionesPg)
    assert opciones["indemnizaciones"]._pool is pool
    assert opciones["transacciones"] is not None
    assert opciones["pagos"] is pagos and pagos._base_url == "http://adaptador-pagos:8080"
    assert opciones["reloj"].ahora().utcoffset() is not None
    claves = ("intervalo_s", "antiguedad_min_s", "lote", "reclamo_s", "alerta_tras_s")
    assert {k: opciones[k] for k in claves} == {
        "intervalo_s": 30,
        "antiguedad_min_s": 600,
        "lote": 4,
        "reclamo_s": 45,
        "alerta_tras_s": 7200,
    }
    assert tarea.done() and not tarea.cancelled()
    assert Bitacora.eventos.index("conciliacion parado") < Bitacora.eventos.index("pool cerrado")
    assert logs.de_nivel("ERROR") == []


def test_la_conciliacion_apagada_no_crea_la_tarea(
    crear: Callable[..., tuple[FastAPI, Any]], aws: dict[str, list[Any]]
) -> None:
    app, _ = crear(indemnizacion_conciliacion_habilitada=False)
    with TestClient(app) as cliente:
        assert cliente.get("/health").json() == {"status": "ok"}
        assert getattr(app.state, "conciliacion_indemnizaciones", None) is None
    assert ConciliacionFalsa.creados == []


def test_sin_base_la_conciliacion_no_arranca(
    crear: Callable[..., tuple[FastAPI, Any]], aws: dict[str, list[Any]]
) -> None:
    app, _ = crear(con_base=False)
    with TestClient(app) as cliente:
        assert cliente.get("/health").json() == {"status": "ok"}
        assert getattr(app.state, "conciliacion_indemnizaciones", None) is None
    assert ConciliacionFalsa.creados == []


def test_una_conciliacion_que_falla_no_tumba_la_aplicacion(
    crear: Callable[..., tuple[FastAPI, Any]], aws: dict[str, list[Any]]
) -> None:
    ConciliacionFalsa.comportamiento = "falla"
    app, logs = crear()
    with TestClient(app) as cliente:
        tarea = app.state.conciliacion_indemnizaciones
        _esperar(lambda: tarea.done())
        assert cliente.get("/health").json() == {"status": "ok"}
    [error] = [
        r
        for r in logs.de_nivel("ERROR")
        if r["message"] == main.MENSAJE_CONCILIACION_INDEMNIZACIONES_DETENIDA
    ]
    assert error["error_type"] == "RuntimeError"
    assert "datos sensibles" not in logs.texto()


def test_la_parada_de_la_conciliacion_espera_con_limite_y_luego_cancela(
    crear: Callable[..., tuple[FastAPI, Any]],
    aws: dict[str, list[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ConciliacionFalsa.comportamiento = "ignora_parada"
    monkeypatch.setattr(main, "PARADA_CONCILIACION_S", 0.05)
    app, logs = crear()
    with TestClient(app):
        tarea = app.state.conciliacion_indemnizaciones
    assert tarea.cancelled()
    [aviso] = [
        r
        for r in logs.de_nivel("WARNING")
        if r["message"] == main.MENSAJE_CONCILIACION_INDEMNIZACIONES_SIN_PARAR
    ]
    assert aviso["limit_s"] == 0.05


# --- La ruta de lectura ----------------------------------------------------------------------------
def test_con_base_la_lectura_queda_armada_sobre_el_pool(
    crear: Callable[..., tuple[FastAPI, Any]], aws: dict[str, list[Any]]
) -> None:
    app, _ = crear()
    with TestClient(app):
        assert app.state.consultar._lectura._pool is app.state.pool
    sin_base, _ = crear(con_base=False)
    with TestClient(sin_base):
        assert getattr(sin_base.state, "consultar", None) is None
