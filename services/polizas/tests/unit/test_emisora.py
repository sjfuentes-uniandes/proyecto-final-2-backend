"""Tarea emisora de polizas (HU-W17, 17.8; ADR 0014, decisión 1): configuración y ciclo de vida.

Encendida por defecto: corre como tarea del ciclo de vida sobre el pool, con el almacén de
documentos y el cliente de adaptador-firma. Sin base, sin bucket o sin la URL de adaptador-firma no
arranca (WARNING) y la app sigue sirviendo. Al cerrar, en dos fases: el consumidor y la tarea
emisora a la vez, y después el relay; todo dentro del stopTimeout de 30 s de ECS.
"""

import asyncio
import io
import json
import time
from collections.abc import Callable, Iterator
from typing import Any, ClassVar, Self

import psycopg_pool
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from polizas import main
from polizas.adapters.outbound import decisiones_pg
from polizas.adapters.outbound.documento_pdf import GeneradorPdf
from polizas.adapters.outbound.firma_http import AdaptadorFirmaHttp
from polizas.adapters.outbound.outbox_pg import OutboxPolizasPg
from polizas.adapters.outbound.polizas_pg import PolizasPg, TransaccionesPg
from polizas.adapters.outbound.reloj import RelojSistema
from polizas.application.poliza.documento import ReunirDatosDocumento
from polizas.config import Settings

COLA = "https://sqs.us-east-1.amazonaws.com/000000000000/solventa-int-polizas-recaudos"
TOPICO = "arn:aws:sns:us-east-1:000000000000:solventa-int-eventos-negocio"
FIRMA_URL = "http://adaptador-firma.solventa.local:8080"
_CON_ALIAS = (
    "SQS_POLIZAS_RECAUDOS_URL",
    "OUTBOX_ENABLED",
    "EVENTS_TOPIC_ARN",
    "AWS_REGION",
    "AWS_ENDPOINT_URL",
    "POLIZAS_BUCKET",
    "ADAPTADOR_FIRMA_URL",
)
_SIN_ALIAS = (
    "EMISION_HABILITADA",
    "EMISION_INTERVALO_S",
    "EMISION_LOTE",
    "EMISION_REINTENTO_S",
    "EMISION_ALERTA_TRAS_S",
    "EMISION_RESERVA_S",
    "ADAPTADOR_FIRMA_TIMEOUT_MS",
)


@pytest.fixture(autouse=True)
def sin_entorno(monkeypatch: pytest.MonkeyPatch) -> None:
    for nombre in _CON_ALIAS:
        monkeypatch.delenv(nombre, raising=False)
    for nombre in _CON_ALIAS + _SIN_ALIAS:
        monkeypatch.delenv(f"SOLVENTA_{nombre}", raising=False)


# --- Configuración ---------------------------------------------------------------------------------
def test_por_omision_encendida_cada_segundo_con_reintento_y_alerta() -> None:
    settings = Settings()
    assert settings.emision_habilitada is True
    assert settings.emision_intervalo_s == 1.0
    assert settings.emision_lote == 10
    assert settings.emision_reintento_s == 30
    # ADR 0014, decisión 1: pasada una hora se mide como error.
    assert settings.emision_alerta_tras_s == 3600
    # 17.8 (ajuste): más que el peor intento (S3 con dos intentos de 6 s, firma de 6,5 s).
    assert settings.emision_reserva_s == 60
    # Mayor que ALLY_FIRMA_TIMEOUT_MS (5000) de adaptador-firma: así llega su 504.
    assert settings.adaptador_firma_timeout_ms == 6500


def test_variantes_solventa(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOLVENTA_EMISION_HABILITADA", "false")
    monkeypatch.setenv("SOLVENTA_EMISION_INTERVALO_S", "0.5")
    monkeypatch.setenv("SOLVENTA_EMISION_LOTE", "3")
    monkeypatch.setenv("SOLVENTA_EMISION_REINTENTO_S", "60")
    monkeypatch.setenv("SOLVENTA_EMISION_ALERTA_TRAS_S", "7200")
    monkeypatch.setenv("SOLVENTA_ADAPTADOR_FIRMA_TIMEOUT_MS", "7000")
    monkeypatch.setenv("SOLVENTA_EMISION_RESERVA_S", "90")
    settings = Settings()
    assert settings.emision_reserva_s == 90
    assert settings.emision_habilitada is False
    assert (settings.emision_intervalo_s, settings.emision_lote) == (0.5, 3)
    assert (settings.emision_reintento_s, settings.emision_alerta_tras_s) == (60, 7200)
    assert settings.adaptador_firma_timeout_ms == 7000


def test_la_url_de_adaptador_firma_como_la_pone_ecs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADAPTADOR_FIRMA_URL", FIRMA_URL)
    assert Settings().adaptador_firma_url == FIRMA_URL


@pytest.mark.parametrize(
    "campo",
    [
        {"emision_intervalo_s": 0},
        {"emision_intervalo_s": 61},
        {"emision_lote": 0},
        {"emision_lote": 51},
        {"emision_reintento_s": 14},
        {"emision_alerta_tras_s": 0},
        {"emision_reserva_s": 29},
        {"adaptador_firma_timeout_ms": 5000},
        {"adaptador_firma_timeout_ms": 10001},
    ],
)
def test_valores_acotados(campo: dict[str, Any]) -> None:
    # Reintento: más que un intento completo (firma de 6,5 s y subida), para que otra tarea no
    # retome una póliza en curso. Timeout: por encima del del adaptador (5 s).
    with pytest.raises(ValueError):
        Settings(**campo)


def test_la_parada_conjunta_cabe_en_el_stop_timeout_de_ecs() -> None:
    # infra/apps/ecs.tf:141: stopTimeout = 30 s. Fase 1: consumidor y emisora en paralelo (dura lo
    # que el mayor); fase 2: el relay. 5 s de margen para uvicorn y los cierres.
    assert (main.PARADA_CONSUMIDOR_S, main.PARADA_EMISION_S, main.PARADA_RELAY_S) == (15, 15, 10)
    assert max(main.PARADA_CONSUMIDOR_S, main.PARADA_EMISION_S) + main.PARADA_RELAY_S <= 25
    # La póliza en curso al parar cabe: la firma (6,5 s) más una subida a S3 (1 + 5 s) y la base.
    settings = Settings()
    assert main.PARADA_EMISION_S > settings.adaptador_firma_timeout_ms / 1000 + 6
    # 17.8 (ajuste): una póliza cortada al parar queda reservada; la retoma otra tarea al vencer.
    assert settings.emision_reserva_s > main.PARADA_EMISION_S
    assert main.PARADA_EMISION_S > settings.emision_intervalo_s


# --- Ciclo de vida ---------------------------------------------------------------------------------
class PoolFalso:
    def __init__(self, url: str = "", **argumentos: Any) -> None:
        self.abierto = False

    async def open(self) -> None:
        self.abierto = True

    async def close(self) -> None:
        self.abierto = False

    def connection(self) -> Any:
        class Conexion:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *_: object) -> None:
                return None

            async def execute(self, sql: str) -> None:
                return None

        return Conexion()


ORDEN: list[str] = []


class EmisoraFalsa:
    """Sustituye a EmitirPolizas: registra con qué se construyó y cuándo se detuvo."""

    creadas: ClassVar[list["EmisoraFalsa"]] = []
    comportamiento: ClassVar[str] = "espera"
    demora_s: ClassVar[float] = 0.0

    def __init__(self, **opciones: Any) -> None:
        self.opciones = opciones
        self.parada_vista = False
        self.cliente_abierto_al_parar: bool | None = None
        EmisoraFalsa.creadas.append(self)

    async def ejecutar(self, parada: asyncio.Event) -> None:
        if EmisoraFalsa.comportamiento == "falla":
            raise RuntimeError("fallo inesperado de la emisora")
        if EmisoraFalsa.comportamiento == "ignora_parada":
            await asyncio.sleep(3600)
        await parada.wait()
        ORDEN.append("emisora:señal")
        await asyncio.sleep(EmisoraFalsa.demora_s)
        self.parada_vista = True
        firma = self.opciones["firma"]
        self.cliente_abierto_al_parar = not firma._cliente.is_closed
        ORDEN.append("emisora:fin")


class ConsumidorFalso:
    demora_s: ClassVar[float] = 0.0

    def __init__(self, *_: Any, **__: Any) -> None:
        return None

    async def ejecutar(self, parada: asyncio.Event) -> None:
        await parada.wait()
        ORDEN.append("consumidor:señal")
        await asyncio.sleep(ConsumidorFalso.demora_s)
        ORDEN.append("consumidor:fin")


class RelayFalso:
    def __init__(self, *_: Any, **__: Any) -> None:
        return None

    async def ejecutar(self, parada: asyncio.Event) -> None:
        await parada.wait()
        ORDEN.append("relay:señal")


ALMACEN = object()


@pytest.fixture
def emisora_falsa(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[EmisoraFalsa]]:
    EmisoraFalsa.creadas = []
    EmisoraFalsa.comportamiento = "espera"
    EmisoraFalsa.demora_s = 0.0
    ORDEN.clear()
    monkeypatch.setattr(main, "EmitirPolizas", EmisoraFalsa)
    yield EmisoraFalsa


@pytest.fixture
def almacenes(monkeypatch: pytest.MonkeyPatch) -> list[Settings]:
    """`almacen_desde` sin S3: devuelve ALMACEN si hay bucket (y None si no)."""
    llamadas: list[Settings] = []

    def almacen_desde(settings: Settings) -> object | None:
        llamadas.append(settings)
        return ALMACEN if settings.polizas_bucket else None

    monkeypatch.setattr(main, "almacen_desde", almacen_desde)
    return llamadas


@pytest.fixture
def crear(
    monkeypatch: pytest.MonkeyPatch, capturar_logs: Callable[..., io.StringIO]
) -> Callable[..., tuple[FastAPI, io.StringIO]]:
    monkeypatch.setattr(decisiones_pg, "migrar", lambda url: None)
    monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", PoolFalso)

    def _crear(con_base: bool = True, **cambios: Any) -> tuple[FastAPI, io.StringIO]:
        configuracion = {"POLIZAS_BUCKET": "solventa-int-polizas", "ADAPTADOR_FIRMA_URL": FIRMA_URL}
        settings = Settings(
            DB_HOST="rds.interno" if con_base else None, **(configuracion | cambios)
        )
        app = main.crear_app(settings)
        return app, capturar_logs()

    return _crear


def _logs(salida: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(linea) for linea in salida.getvalue().splitlines() if linea.strip()]


def _mensajes(salida: io.StringIO, nivel: str) -> list[dict[str, Any]]:
    return [log for log in _logs(salida) if log["level"] == nivel]


def test_arranca_sobre_el_pool_con_el_almacen_y_el_cliente_de_firma(
    crear: Callable[..., tuple[FastAPI, io.StringIO]],
    emisora_falsa: type[EmisoraFalsa],
    almacenes: list[Settings],
) -> None:
    app, salida = crear(
        emision_intervalo_s=0.5,
        emision_lote=4,
        emision_reintento_s=45,
        emision_alerta_tras_s=1800,
        emision_reserva_s=120,
        adaptador_firma_timeout_ms=7000,
    )
    with TestClient(app) as cliente:
        tarea = app.state.emisora_polizas
        assert isinstance(tarea, asyncio.Task) and not tarea.done()
        assert cliente.get("/health").json() == {"status": "ok"}
        pool = app.state.pool
    [emisora] = emisora_falsa.creadas
    opciones = emisora.opciones
    assert isinstance(opciones["polizas"], PolizasPg)
    assert isinstance(opciones["transacciones"], TransaccionesPg)
    assert opciones["transacciones"]._pool is pool
    assert isinstance(opciones["datos"], ReunirDatosDocumento)
    assert isinstance(opciones["generador"], GeneradorPdf)
    assert opciones["almacen"] is ALMACEN
    assert isinstance(opciones["eventos"], OutboxPolizasPg)
    assert isinstance(opciones["reloj"], RelojSistema)
    firma = opciones["firma"]
    assert isinstance(firma, AdaptadorFirmaHttp)
    assert (firma._base_url, firma._timeout_ms) == (FIRMA_URL, 7000)
    claves = ("intervalo_s", "lote", "reintento_s", "reserva_s", "alerta_tras_s")
    assert {k: opciones[k] for k in claves} == {
        "intervalo_s": 0.5,
        "lote": 4,
        "reintento_s": 45,
        "reserva_s": 120,
        "alerta_tras_s": 1800,
    }
    # Se detuvo por la señal, con el cliente de firma todavía abierto; después se cerró.
    assert tarea.done() and not tarea.cancelled()
    assert (emisora.parada_vista, emisora.cliente_abierto_al_parar) == (True, True)
    assert firma._cliente.is_closed
    assert _mensajes(salida, "WARNING") == _mensajes(salida, "ERROR") == []


def test_con_puertos_inyectados_usa_el_pool_y_el_reloj_dados(
    emisora_falsa: type[EmisoraFalsa], almacenes: list[Settings], dobles: Any
) -> None:
    pool = PoolFalso("postgresql://prueba")
    settings = Settings(POLIZAS_BUCKET="b", ADAPTADOR_FIRMA_URL=FIRMA_URL)
    app = main.crear_app(settings, puertos=dobles.puertos(), pool=pool)
    with TestClient(app):
        assert app.state.emisora_polizas is not None
    [emisora] = emisora_falsa.creadas
    assert emisora.opciones["transacciones"]._pool is pool
    assert emisora.opciones["reloj"] is dobles.reloj


@pytest.mark.parametrize(
    ("cambios", "falta"),
    [
        ({"con_base": False}, "base"),
        ({"POLIZAS_BUCKET": None}, "POLIZAS_BUCKET"),
        ({"ADAPTADOR_FIRMA_URL": ""}, "ADAPTADOR_FIRMA_URL"),
        ({"ADAPTADOR_FIRMA_URL": "  "}, "ADAPTADOR_FIRMA_URL"),
    ],
    ids=["sin_base", "sin_bucket", "sin_url", "url_en_blanco"],
)
def test_sin_configuracion_no_arranca_y_avisa(
    crear: Callable[..., tuple[FastAPI, io.StringIO]],
    emisora_falsa: type[EmisoraFalsa],
    almacenes: list[Settings],
    cambios: dict[str, Any],
    falta: str,
) -> None:
    app, salida = crear(**cambios)
    with TestClient(app) as cliente:
        assert cliente.get("/health").json() == {"status": "ok"}
        assert getattr(app.state, "emisora_polizas", None) is None
    assert emisora_falsa.creadas == []
    [aviso] = _mensajes(salida, "WARNING")
    assert (aviso["message"], aviso["missing"]) == (main.MENSAJE_EMISION_SIN_CONFIGURACION, falta)
    assert _mensajes(salida, "ERROR") == []


def test_apagada_no_arranca_ni_avisa(
    crear: Callable[..., tuple[FastAPI, io.StringIO]],
    emisora_falsa: type[EmisoraFalsa],
    almacenes: list[Settings],
) -> None:
    app, salida = crear(emision_habilitada=False)
    with TestClient(app):
        assert getattr(app.state, "emisora_polizas", None) is None
    assert emisora_falsa.creadas == [] and almacenes == []
    assert _mensajes(salida, "WARNING") == []


def test_si_la_tarea_falla_la_app_sigue_sirviendo(
    crear: Callable[..., tuple[FastAPI, io.StringIO]],
    emisora_falsa: type[EmisoraFalsa],
    almacenes: list[Settings],
) -> None:
    emisora_falsa.comportamiento = "falla"
    app, salida = crear()
    with TestClient(app) as cliente:
        tarea = app.state.emisora_polizas
        _esperar(tarea.done)
        assert cliente.get("/health").json() == {"status": "ok"}
    [error] = _mensajes(salida, "ERROR")
    assert (error["message"], error["error_type"]) == (
        main.MENSAJE_EMISION_DETENIDA,
        "RuntimeError",
    )
    assert "fallo inesperado" not in salida.getvalue()


def test_la_parada_espera_con_limite_y_luego_cancela(
    crear: Callable[..., tuple[FastAPI, io.StringIO]],
    emisora_falsa: type[EmisoraFalsa],
    almacenes: list[Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emisora_falsa.comportamiento = "ignora_parada"
    monkeypatch.setattr(main, "PARADA_EMISION_S", 0.05)
    app, salida = crear()
    inicio = time.monotonic()
    with TestClient(app):
        tarea = app.state.emisora_polizas
    assert time.monotonic() - inicio < 5
    assert tarea.cancelled()
    [aviso] = _mensajes(salida, "WARNING")
    assert (aviso["message"], aviso["limit_s"]) == (main.MENSAJE_EMISION_SIN_PARAR, 0.05)


def test_la_parada_es_conjunta_consumidor_y_emisora_a_la_vez_y_despues_el_relay(
    crear: Callable[..., tuple[FastAPI, io.StringIO]],
    emisora_falsa: type[EmisoraFalsa],
    almacenes: list[Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main, "ConsumidorInbox", ConsumidorFalso)
    monkeypatch.setattr(main, "ReceptorSqs", lambda *a, **k: object())
    monkeypatch.setattr(main, "cliente_sqs", lambda *a, **k: object())
    monkeypatch.setattr(main, "RelayOutbox", RelayFalso)
    monkeypatch.setattr(main, "cliente_sns", lambda *a, **k: object())
    EmisoraFalsa.demora_s = 0.6
    ConsumidorFalso.demora_s = 0.5
    app, salida = crear(SQS_POLIZAS_RECAUDOS_URL=COLA, OUTBOX_ENABLED=True, EVENTS_TOPIC_ARN=TOPICO)
    inicio = time.monotonic()
    with TestClient(app):
        tareas = (app.state.consumidor_recaudos, app.state.emisora_polizas, app.state.relay_outbox)
        inicio = time.monotonic()
    duracion = time.monotonic() - inicio
    assert all(t.done() and not t.cancelled() for t in tareas)
    # Fase 1: las dos señales antes de que termine cualquiera (en paralelo, no en serie).
    assert set(ORDEN[:2]) == {"consumidor:señal", "emisora:señal"}
    # Fase 2: el relay, solo después de las dos (publica lo que dejaron en el outbox).
    assert ORDEN[-1] == "relay:señal"
    assert ORDEN.index("relay:señal") > max(
        ORDEN.index("consumidor:fin"), ORDEN.index("emisora:fin")
    )
    # En paralelo dura lo que la más lenta (0,6 s), no la suma (1,1 s).
    assert duracion < 1.0
    assert _mensajes(salida, "WARNING") == _mensajes(salida, "ERROR") == []


def _esperar(condicion: Callable[[], bool], limite_s: float = 5) -> None:
    fin = time.monotonic() + limite_s
    while not condicion():
        assert time.monotonic() < fin, "la condición no se cumplió a tiempo"
        time.sleep(0.01)
