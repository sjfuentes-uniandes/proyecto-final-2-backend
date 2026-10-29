"""Configuración y ciclo de vida del consumidor de `OfertaAceptada` y del relay del outbox
(HU-W16, 16.10).

La infraestructura entrega a dinero (`infra/apps/ecs.tf` y `platform/catalog.tf`):
`ADAPTADOR_PAGOS_URL` (calls), `SQS_DINERO_ACEPTACIONES_URL` (consumes), `OUTBOX_ENABLED` y
`EVENTS_TOPIC_ARN` (publishes) y `AWS_REGION`. En local no llegan: ni consumidor ni relay.

- Consumidor activo si llega la cola, con lote 2 y espera larga de 10 s.
- Relay como en polizas (16.6): apagado por defecto.
- Al cerrar se detiene el consumidor, después el relay y al final se cierra el pool, cada tarea con
  su límite; la suma cabe en los 30 s del `stopTimeout` de ECS.
- Sin cola, tópico, base o cliente, o si una tarea falla, se registra y la app sigue sirviendo.
"""

import asyncio
import time
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any, ClassVar

import psycopg_pool
import pytest
from dinero import main
from dinero.adapters.outbound import recaudos_pg
from dinero.config import TIMEOUT_ADAPTADOR_PAGOS_MS, Settings
from fastapi import FastAPI
from fastapi.testclient import TestClient
from solventa_common import inbox, outbox

COLA = "https://sqs.us-east-1.amazonaws.com/000000000000/solventa-int-dinero-aceptaciones"
TOPICO = "arn:aws:sns:us-east-1:000000000000:solventa-int-eventos-negocio"
_CON_ALIAS = (
    "ADAPTADOR_PAGOS_URL",
    "SQS_DINERO_ACEPTACIONES_URL",
    "OUTBOX_ENABLED",
    "EVENTS_TOPIC_ARN",
    "AWS_REGION",
    "AWS_ENDPOINT_URL",
)
_SIN_ALIAS = (
    "ADAPTADOR_PAGOS_TIMEOUT_MS",
    "CONSUMIDOR_MAX_MENSAJES",
    "CONSUMIDOR_ESPERA_LARGA_S",
    "OUTBOX_LOTE",
    "OUTBOX_INTERVALO_S",
    "OUTBOX_MAX_INTENTOS",
)


@pytest.fixture(autouse=True)
def sin_entorno(monkeypatch: pytest.MonkeyPatch) -> None:
    for nombre in _CON_ALIAS:
        monkeypatch.delenv(nombre, raising=False)
    for nombre in _CON_ALIAS + _SIN_ALIAS:
        monkeypatch.delenv(f"SOLVENTA_{nombre}", raising=False)


# --- Configuración ---------------------------------------------------------------------------------
def test_por_omision() -> None:
    settings = Settings()
    assert settings.adaptador_pagos_url == "http://localhost:8091"
    assert settings.adaptador_pagos_timeout_ms == 6500
    assert settings.sqs_dinero_aceptaciones_url is None
    assert (settings.consumidor_max_mensajes, settings.consumidor_espera_larga_s) == (2, 10)
    assert settings.outbox_enabled is False and settings.events_topic_arn is None
    assert (settings.outbox_lote, settings.outbox_intervalo_s, settings.outbox_max_intentos) == (
        outbox.LOTE,
        outbox.INTERVALO_S,
        outbox.MAX_INTENTOS,
    )
    assert (settings.aws_region, settings.aws_endpoint_url) == ("us-east-1", None)


def test_las_variables_como_las_pone_ecs(monkeypatch: pytest.MonkeyPatch) -> None:
    for nombre, valor in {
        "ADAPTADOR_PAGOS_URL": "http://adaptador-pagos:8080",
        "SQS_DINERO_ACEPTACIONES_URL": COLA,
        "OUTBOX_ENABLED": "true",
        "EVENTS_TOPIC_ARN": TOPICO,
        "AWS_REGION": "us-east-2",
    }.items():
        monkeypatch.setenv(nombre, valor)
    settings = Settings()
    assert settings.adaptador_pagos_url == "http://adaptador-pagos:8080"
    assert settings.sqs_dinero_aceptaciones_url == COLA
    assert (settings.outbox_enabled, settings.events_topic_arn) == (True, TOPICO)
    assert settings.aws_region == "us-east-2"


def test_variantes_solventa_como_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOLVENTA_ADAPTADOR_PAGOS_URL", "http://localhost:9999")
    monkeypatch.setenv("SOLVENTA_ADAPTADOR_PAGOS_TIMEOUT_MS", "7000")
    monkeypatch.setenv("SOLVENTA_SQS_DINERO_ACEPTACIONES_URL", COLA)
    monkeypatch.setenv("SOLVENTA_AWS_ENDPOINT_URL", "http://localstack:4566")
    monkeypatch.setenv("SOLVENTA_CONSUMIDOR_ESPERA_LARGA_S", "1")
    settings = Settings()
    assert (settings.adaptador_pagos_url, settings.adaptador_pagos_timeout_ms) == (
        "http://localhost:9999",
        7000,
    )
    assert settings.sqs_dinero_aceptaciones_url == COLA
    assert settings.aws_endpoint_url == "http://localstack:4566"
    assert settings.consumidor_espera_larga_s == 1


def test_el_timeout_de_dinero_supera_al_del_adaptador() -> None:
    """adaptador-pagos corta a ALLY_PAGOS_TIMEOUT_MS (5000, infra/apps/variables.tf): si dinero
    cortara antes o a la vez, perdería la respuesta definitiva (o el 504) del adaptador."""
    assert TIMEOUT_ADAPTADOR_PAGOS_MS == 5000
    assert Settings().adaptador_pagos_timeout_ms > TIMEOUT_ADAPTADOR_PAGOS_MS
    with pytest.raises(ValueError):
        Settings(adaptador_pagos_timeout_ms=TIMEOUT_ADAPTADOR_PAGOS_MS)
    assert Settings(adaptador_pagos_timeout_ms=5001).adaptador_pagos_timeout_ms == 5001


@pytest.mark.parametrize(
    "campo",
    [
        {"adaptador_pagos_timeout_ms": 10001},
        {"consumidor_max_mensajes": 0},
        {"consumidor_max_mensajes": 3},
        {"consumidor_espera_larga_s": 0},
        {"consumidor_espera_larga_s": 11},
        {"outbox_lote": 0},
        {"outbox_max_intentos": 0},
        {"outbox_intervalo_s": 0},
    ],
)
def test_limites_de_la_configuracion(campo: dict[str, int]) -> None:
    """Lote y espera acotados a lo que cabe en la parada; el timeout, en la visibilidad (60 s)."""
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
    """Registra con qué se construyó; `comportamiento` decide qué hace `ejecutar`."""

    comportamiento: ClassVar[str] = "espera"
    nombre: ClassVar[str] = ""

    def __init__(self, *argumentos: Any, **opciones: Any) -> None:
        self.argumentos = argumentos
        self.opciones = opciones
        self.parada_vista = False
        type(self).creados.append(self)  # type: ignore[attr-defined]

    async def ejecutar(self, parada: asyncio.Event) -> None:
        if type(self).comportamiento == "falla":
            raise RuntimeError("fallo inesperado con datos sensibles")
        if type(self).comportamiento == "ignora_parada":
            await asyncio.sleep(3600)
        await parada.wait()
        self.parada_vista = True
        Bitacora.eventos.append(f"{type(self).nombre} parado")


class RelayFalso(Tarea):
    creados: ClassVar[list["RelayFalso"]] = []
    nombre = "relay"


class ConsumidorFalso(Tarea):
    creados: ClassVar[list["ConsumidorFalso"]] = []
    nombre = "consumidor"


def _cliente_sqs_falso(read_timeout: float) -> Any:
    return SimpleNamespace(meta=SimpleNamespace(config=SimpleNamespace(read_timeout=read_timeout)))


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    llamadas: dict[str, list[Any]] = {"sns": [], "sqs": []}

    def sns(region: str, *, endpoint_url: str | None = None) -> object:
        llamadas["sns"].append((region, endpoint_url))
        return object()

    def sqs(region: str, *, endpoint_url: str | None = None, espera_larga_s: int) -> Any:
        llamadas["sqs"].append((region, endpoint_url, espera_larga_s))
        return _cliente_sqs_falso(espera_larga_s + inbox.MARGEN_LECTURA_S)

    monkeypatch.setattr(main, "cliente_sns", sns)
    monkeypatch.setattr(main, "cliente_sqs", sqs)
    return llamadas


@pytest.fixture
def tareas(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    Bitacora.eventos = []
    for clase in (RelayFalso, ConsumidorFalso):
        clase.creados = []
        clase.comportamiento = "espera"
    monkeypatch.setattr(main, "RelayOutbox", RelayFalso)
    monkeypatch.setattr(main, "ConsumidorInbox", ConsumidorFalso)
    yield


@pytest.fixture
def crear(
    monkeypatch: pytest.MonkeyPatch, capturar_logs: Callable[[], Any]
) -> Callable[..., tuple[FastAPI, Any]]:
    """La app con la base configurada (pool falso) y los logs capturados tras instrumentar."""
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


# --- Consumidor ------------------------------------------------------------------------------------
@pytest.mark.usefixtures("tareas")
def test_sin_cola_no_hay_consumidor_y_se_avisa(
    crear: Callable[..., tuple[FastAPI, Any]], aws: dict[str, list[Any]]
) -> None:
    app, logs = crear()
    with TestClient(app) as cliente:
        assert cliente.get("/health").json() == {"status": "ok"}
        assert getattr(app.state, "consumidor_ofertas", None) is None
    assert ConsumidorFalso.creados == [] and aws["sqs"] == []
    [aviso] = logs.de_nivel("WARNING")
    assert (aviso["message"], aviso["missing"]) == (
        main.MENSAJE_CONSUMIDOR_SIN_COLA,
        "SQS_DINERO_ACEPTACIONES_URL",
    )
    assert logs.de_nivel("ERROR") == []


@pytest.mark.usefixtures("tareas")
def test_con_cola_consume_con_lote_2_y_espera_10_y_se_detiene_antes_del_pool(
    crear: Callable[..., tuple[FastAPI, Any]], aws: dict[str, list[Any]]
) -> None:
    app, logs = crear(
        SQS_DINERO_ACEPTACIONES_URL=COLA,
        AWS_REGION="us-east-2",
        AWS_ENDPOINT_URL="http://localstack:4566",
    )
    with TestClient(app) as cliente:
        tarea = app.state.consumidor_ofertas
        assert isinstance(tarea, asyncio.Task) and not tarea.done()
        assert cliente.get("/health").json() == {"status": "ok"}
        pool = app.state.pool
        cliente_pagos = app.state.cliente_pagos
        assert not cliente_pagos.is_closed
    [consumidor] = ConsumidorFalso.creados
    receptor, repositorio, manejador = consumidor.argumentos
    assert isinstance(receptor, inbox.ReceptorSqs)
    assert (receptor.max_mensajes, receptor.espera_larga_s) == (2, 10)
    assert receptor._cola_url == COLA
    assert aws["sqs"] == [("us-east-2", "http://localstack:4566", 10)]
    assert isinstance(repositorio, inbox.InboxPg) and repositorio._pool is pool
    assert callable(manejador)
    assert consumidor.opciones["reloj"]().utcoffset() is not None
    # Se detuvo por la señal (no cancelado), antes de cerrar el pool; el cliente HTTP, cerrado.
    assert tarea.done() and not tarea.cancelled()
    assert Bitacora.eventos == ["consumidor parado", "pool cerrado"]
    assert cliente_pagos.is_closed
    assert logs.de_nivel("ERROR") == [] and logs.de_nivel("WARNING") == []


@pytest.mark.usefixtures("tareas")
def test_el_cliente_del_adaptador_usa_la_url_y_el_timeout_configurados(
    crear: Callable[..., tuple[FastAPI, Any]], aws: dict[str, list[Any]]
) -> None:
    app, _ = crear(
        SQS_DINERO_ACEPTACIONES_URL=COLA,
        ADAPTADOR_PAGOS_URL="http://adaptador-pagos:8080",
        adaptador_pagos_timeout_ms=7000,
    )
    with TestClient(app):
        pagos = app.state.pagos
    assert pagos._base_url == "http://adaptador-pagos:8080"
    assert pagos._timeout_s == 7.0


@pytest.mark.usefixtures("tareas")
def test_con_cola_y_sin_base_arranca_y_lo_registra(
    crear: Callable[..., tuple[FastAPI, Any]], aws: dict[str, list[Any]]
) -> None:
    app, logs = crear(con_base=False, SQS_DINERO_ACEPTACIONES_URL=COLA)
    with TestClient(app) as cliente:
        assert cliente.get("/health").json() == {"status": "ok"}
    assert ConsumidorFalso.creados == []
    [error] = logs.de_nivel("ERROR")
    assert (error["message"], error["missing"]) == (
        main.MENSAJE_CONSUMIDOR_SIN_CONFIGURACION,
        "base",
    )


@pytest.mark.usefixtures("tareas")
def test_si_no_se_puede_crear_el_cliente_sqs_arranca_y_lo_registra(
    crear: Callable[..., tuple[FastAPI, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def sqs(region: str, *, endpoint_url: str | None = None, espera_larga_s: int) -> Any:
        raise ValueError(f"Invalid endpoint: {endpoint_url}")

    monkeypatch.setattr(main, "cliente_sqs", sqs)
    app, logs = crear(SQS_DINERO_ACEPTACIONES_URL=COLA, AWS_ENDPOINT_URL="no-url")
    with TestClient(app) as cliente:
        assert cliente.get("/health").json() == {"status": "ok"}
    assert ConsumidorFalso.creados == []
    [error] = logs.de_nivel("ERROR")
    assert (error["message"], error["error_type"]) == (
        main.MENSAJE_CONSUMIDOR_NO_ARRANCO,
        "ValueError",
    )
    assert "no-url" not in logs.texto()


@pytest.mark.usefixtures("tareas")
def test_un_receptor_que_rechaza_el_cliente_arranca_y_lo_registra(
    crear: Callable[..., tuple[FastAPI, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Un cliente cuyo timeout de lectura no supera la espera larga: ReceptorSqs lo rechaza.
    monkeypatch.setattr(main, "cliente_sqs", lambda *a, **k: _cliente_sqs_falso(1))
    app, logs = crear(SQS_DINERO_ACEPTACIONES_URL=COLA)
    with TestClient(app) as cliente:
        assert cliente.get("/health").json() == {"status": "ok"}
    [error] = logs.de_nivel("ERROR")
    assert (error["message"], error["error_type"]) == (
        main.MENSAJE_CONSUMIDOR_NO_ARRANCO,
        "ValueError",
    )


@pytest.mark.usefixtures("tareas")
def test_un_consumidor_que_falla_no_tumba_la_aplicacion(
    crear: Callable[..., tuple[FastAPI, Any]], aws: dict[str, list[Any]]
) -> None:
    ConsumidorFalso.comportamiento = "falla"
    app, logs = crear(SQS_DINERO_ACEPTACIONES_URL=COLA)
    with TestClient(app) as cliente:
        tarea = app.state.consumidor_ofertas
        _esperar(lambda: tarea.done())
        assert cliente.get("/health").json() == {"status": "ok"}
    [error] = logs.de_nivel("ERROR")
    assert (error["message"], error["error_type"]) == (
        main.MENSAJE_CONSUMIDOR_DETENIDO,
        "RuntimeError",
    )
    assert "datos sensibles" not in logs.texto()


@pytest.mark.usefixtures("tareas")
def test_la_parada_del_consumidor_espera_con_limite_y_luego_cancela(
    crear: Callable[..., tuple[FastAPI, Any]],
    aws: dict[str, list[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ConsumidorFalso.comportamiento = "ignora_parada"
    monkeypatch.setattr(main, "PARADA_CONSUMIDOR_S", 0.05)
    app, logs = crear(SQS_DINERO_ACEPTACIONES_URL=COLA)
    inicio = time.monotonic()
    with TestClient(app):
        tarea = app.state.consumidor_ofertas
    assert time.monotonic() - inicio < 5
    assert tarea.cancelled()
    [aviso] = logs.de_nivel("WARNING")
    assert (aviso["message"], aviso["limit_s"]) == (main.MENSAJE_CONSUMIDOR_SIN_PARAR, 0.05)
    assert Bitacora.eventos == ["pool cerrado"]


# --- Relay -----------------------------------------------------------------------------------------
@pytest.mark.usefixtures("tareas")
def test_relay_deshabilitado_no_crea_la_tarea(
    crear: Callable[..., tuple[FastAPI, Any]], aws: dict[str, list[Any]]
) -> None:
    app, logs = crear(EVENTS_TOPIC_ARN=TOPICO)
    with TestClient(app) as cliente:
        assert cliente.get("/health").json() == {"status": "ok"}
        assert getattr(app.state, "relay_outbox", None) is None
    assert RelayFalso.creados == [] and aws["sns"] == []
    assert logs.de_nivel("ERROR") == []


@pytest.mark.usefixtures("tareas")
def test_relay_habilitado_arranca_sobre_el_pool(
    crear: Callable[..., tuple[FastAPI, Any]], aws: dict[str, list[Any]]
) -> None:
    app, logs = crear(
        OUTBOX_ENABLED=True,
        EVENTS_TOPIC_ARN=TOPICO,
        AWS_REGION="us-east-2",
        AWS_ENDPOINT_URL="http://localstack:4566",
        outbox_lote=7,
        outbox_intervalo_s=0.5,
        outbox_max_intentos=3,
    )
    with TestClient(app) as cliente:
        tarea = app.state.relay_outbox
        assert isinstance(tarea, asyncio.Task) and not tarea.done()
        assert cliente.get("/health").json() == {"status": "ok"}
        pool = app.state.pool
    [relay] = RelayFalso.creados
    repositorio, publicador = relay.argumentos
    assert isinstance(repositorio, outbox.OutboxPg) and repositorio._pool is pool
    assert isinstance(publicador, outbox.PublicadorSns) and publicador._topic_arn == TOPICO
    assert aws["sns"] == [("us-east-2", "http://localstack:4566")]
    assert {k: relay.opciones[k] for k in ("lote", "intervalo_s", "max_intentos")} == {
        "lote": 7,
        "intervalo_s": 0.5,
        "max_intentos": 3,
    }
    assert relay.opciones["reloj"]().utcoffset() is not None
    assert tarea.done() and not tarea.cancelled()
    assert Bitacora.eventos == ["relay parado", "pool cerrado"]
    assert logs.de_nivel("ERROR") == []


@pytest.mark.usefixtures("tareas")
@pytest.mark.parametrize("topico", [None, ""])
def test_relay_habilitado_sin_topico_arranca_y_lo_registra(
    crear: Callable[..., tuple[FastAPI, Any]], aws: dict[str, list[Any]], topico: str | None
) -> None:
    app, logs = crear(OUTBOX_ENABLED=True, EVENTS_TOPIC_ARN=topico)
    with TestClient(app) as cliente:
        assert cliente.get("/health").json() == {"status": "ok"}
    assert RelayFalso.creados == []
    [error] = logs.de_nivel("ERROR")
    assert (error["message"], error["missing"]) == (
        main.MENSAJE_RELAY_SIN_CONFIGURACION,
        "EVENTS_TOPIC_ARN",
    )


@pytest.mark.usefixtures("tareas")
def test_relay_habilitado_sin_base_arranca_y_lo_registra(
    crear: Callable[..., tuple[FastAPI, Any]], aws: dict[str, list[Any]]
) -> None:
    app, logs = crear(con_base=False, OUTBOX_ENABLED=True, EVENTS_TOPIC_ARN=TOPICO)
    with TestClient(app) as cliente:
        assert cliente.get("/health").json() == {"status": "ok"}
    assert RelayFalso.creados == []
    [error] = logs.de_nivel("ERROR")
    assert (error["message"], error["missing"]) == (main.MENSAJE_RELAY_SIN_CONFIGURACION, "base")


@pytest.mark.usefixtures("tareas")
def test_si_no_se_puede_crear_el_cliente_sns_arranca_y_lo_registra(
    crear: Callable[..., tuple[FastAPI, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def sns(region: str, *, endpoint_url: str | None = None) -> object:
        raise ValueError(f"Invalid endpoint: {endpoint_url}")

    monkeypatch.setattr(main, "cliente_sns", sns)
    app, logs = crear(OUTBOX_ENABLED=True, EVENTS_TOPIC_ARN=TOPICO, AWS_ENDPOINT_URL="no-url")
    with TestClient(app) as cliente:
        assert cliente.get("/health").json() == {"status": "ok"}
    assert RelayFalso.creados == []
    [error] = logs.de_nivel("ERROR")
    assert (error["message"], error["error_type"]) == (main.MENSAJE_RELAY_NO_ARRANCO, "ValueError")
    assert "no-url" not in logs.texto()


@pytest.mark.usefixtures("tareas")
def test_un_relay_que_falla_no_tumba_la_aplicacion(
    crear: Callable[..., tuple[FastAPI, Any]], aws: dict[str, list[Any]]
) -> None:
    RelayFalso.comportamiento = "falla"
    app, logs = crear(OUTBOX_ENABLED=True, EVENTS_TOPIC_ARN=TOPICO)
    with TestClient(app) as cliente:
        tarea = app.state.relay_outbox
        _esperar(lambda: tarea.done())
        assert cliente.get("/health").json() == {"status": "ok"}
    [error] = logs.de_nivel("ERROR")
    assert (error["message"], error["error_type"]) == (main.MENSAJE_RELAY_DETENIDO, "RuntimeError")


@pytest.mark.usefixtures("tareas")
def test_la_parada_del_relay_espera_con_limite_y_luego_cancela(
    crear: Callable[..., tuple[FastAPI, Any]],
    aws: dict[str, list[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    RelayFalso.comportamiento = "ignora_parada"
    monkeypatch.setattr(main, "PARADA_RELAY_S", 0.05)
    app, logs = crear(OUTBOX_ENABLED=True, EVENTS_TOPIC_ARN=TOPICO)
    with TestClient(app):
        tarea = app.state.relay_outbox
    assert tarea.cancelled()
    # El otro WARNING es el del consumidor sin cola (no la hay en esta prueba).
    [aviso] = [r for r in logs.de_nivel("WARNING") if r["message"] == main.MENSAJE_RELAY_SIN_PARAR]
    assert aviso["limit_s"] == 0.05


# --- Parada conjunta -------------------------------------------------------------------------------
@pytest.mark.usefixtures("tareas")
def test_se_detiene_el_consumidor_luego_el_relay_y_al_final_el_pool(
    crear: Callable[..., tuple[FastAPI, Any]], aws: dict[str, list[Any]]
) -> None:
    """El último cobro escribe su evento en el outbox: el relay sigue hasta que para el consumidor."""
    app, _ = crear(SQS_DINERO_ACEPTACIONES_URL=COLA, OUTBOX_ENABLED=True, EVENTS_TOPIC_ARN=TOPICO)
    with TestClient(app):
        pass
    assert Bitacora.eventos == ["consumidor parado", "relay parado", "pool cerrado"]


def test_los_limites_de_parada_caben_en_el_stop_timeout_de_ecs() -> None:
    """infra/apps/ecs.tf:141: stopTimeout = 30 s entre SIGTERM y SIGKILL. Se detienen en serie:
    el consumidor (una espera larga de 10 s más un cobro de hasta 6,5 s, con margen) y después el
    relay (un ciclo normal tarda ms). Quedan 5 s para que uvicorn termine las solicitudes y se
    cierre el pool."""
    settings = Settings()
    cobro_s = settings.adaptador_pagos_timeout_ms / 1000
    assert main.PARADA_CONSUMIDOR_S >= settings.consumidor_espera_larga_s + cobro_s
    assert main.PARADA_CONSUMIDOR_S + main.PARADA_RELAY_S <= 25
    assert (main.PARADA_CONSUMIDOR_S, main.PARADA_RELAY_S) == (20, 5)
