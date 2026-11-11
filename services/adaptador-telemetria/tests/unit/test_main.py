"""Armado de la app: archivo, secreto, ciclo de vida y relay del Outbox (HU-W21)."""

import asyncio
import json
import logging
from typing import Any, ClassVar

import pytest
from adaptador_telemetria import main
from adaptador_telemetria.adapters.outbound.archivo_s3 import ArchivoS3, ArchivoSinConfigurar
from adaptador_telemetria.adapters.outbound.recibidos_pg import RegistroPg
from adaptador_telemetria.config import Settings
from adaptador_telemetria.main import crear_app
from fastapi.testclient import TestClient


class PoolInerte:
    """Un pool que nadie usa: armar el servicio no abre conexiones."""


@pytest.fixture(autouse=True)
def credenciales_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")


def construir(caplog: pytest.LogCaptureFixture, *args: Any, **kwargs: Any) -> Any:
    """Crea la app y devuelve a caplog su handler: `instrumentar` reconfigura el logging raíz."""
    app = crear_app(*args, **kwargs)
    logging.getLogger().addHandler(caplog.handler)
    return app


def test_sin_bucket_el_archivo_no_acepta_nada(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR):
        assert isinstance(main._archivo(Settings()), ArchivoSinConfigurar)
    assert any("sin EVIDENCIAS_BUCKET" in r.getMessage() for r in caplog.records)


def test_con_bucket_el_archivo_es_s3_con_el_cifrado_y_el_corte_configurados(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVIDENCIAS_BUCKET", "solventa-local-evidencias")
    monkeypatch.setenv("EVIDENCIAS_BUCKET_SSE", "aws:kms")
    monkeypatch.setenv("SOLVENTA_S3_TIMEOUT_MS", "250")
    archivo = main._archivo(Settings())
    assert isinstance(archivo, ArchivoS3)
    assert (archivo._bucket, archivo._sse, archivo._timeout_s) == (
        "solventa-local-evidencias",
        "aws:kms",
        0.25,
    )


def test_si_el_cliente_s3_falla_el_archivo_no_acepta_nada(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def falla(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("secreto en el mensaje")

    monkeypatch.setenv("EVIDENCIAS_BUCKET", "b")
    monkeypatch.setattr(main, "cliente_s3", falla)
    with caplog.at_level(logging.ERROR):
        assert isinstance(main._archivo(Settings()), ArchivoSinConfigurar)
    registro = next(r for r in caplog.records if r.getMessage() == main.MENSAJE_ARCHIVO_NO_ARRANCO)
    assert registro.error_type == "RuntimeError"  # type: ignore[attr-defined]
    assert "secreto" not in registro.getMessage()


def test_sin_secreto_valido_lo_registra_y_la_app_arranca(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALLY_TELEMETRIA_CREDENTIALS", "no es json")
    with caplog.at_level(logging.ERROR), TestClient(construir(caplog)) as cliente:
        assert cliente.get("/health").status_code == 200
    mensajes = [r.getMessage() for r in caplog.records]
    assert main.MENSAJE_SIN_SECRETO in mensajes
    assert main.MENSAJE_SIN_BASE in mensajes
    assert "no es json" not in " ".join(mensajes)


def test_con_secreto_no_lo_registra(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALLY_TELEMETRIA_CREDENTIALS", json.dumps({"hmac_secret": "s-123"}))
    with caplog.at_level(logging.INFO), TestClient(construir(caplog)):
        pass
    assert main.MENSAJE_SIN_SECRETO not in [r.getMessage() for r in caplog.records]
    assert "s-123" not in caplog.text


def test_con_pool_el_registro_es_el_de_postgresql() -> None:
    app = crear_app(pool=PoolInerte())  # type: ignore[arg-type]
    with TestClient(app):
        assert isinstance(app.state.recibir_evento._registro, RegistroPg)


# --- Base de datos ---------------------------------------------------------------------------------
class PoolFalso:
    eventos: ClassVar[list[str]] = []

    def __init__(self, url: str, **argumentos: Any) -> None:
        self.eventos.append(f"pool:{argumentos['min_size']}-{argumentos['max_size']}")

    async def open(self) -> None:
        self.eventos.append("abre")

    async def close(self) -> None:
        self.eventos.append("cierra")


def test_con_base_migra_abre_el_pool_y_lo_cierra(monkeypatch: pytest.MonkeyPatch) -> None:
    import psycopg_pool

    PoolFalso.eventos = []
    monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", PoolFalso)
    monkeypatch.setattr(
        main,
        "migrar",
        lambda url: PoolFalso.eventos.append(f"migra:{url.split('/')[-1].split('?')[0]}"),
    )
    monkeypatch.setenv("SOLVENTA_DB_HOST", "rds.interno")
    monkeypatch.setenv("SOLVENTA_DB_POOL_SIZE", "7")
    with TestClient(crear_app(Settings())) as cliente:
        assert cliente.get("/health").status_code == 200
        assert PoolFalso.eventos == ["migra:adaptador_telemetria", "pool:1-7", "abre"]
    assert PoolFalso.eventos[-1] == "cierra"


# --- Relay -----------------------------------------------------------------------------------------
class RelayFalso:
    instancias: ClassVar[list["RelayFalso"]] = []

    def __init__(self, repositorio: Any, publicador: Any, **opciones: Any) -> None:
        self.opciones = opciones
        self.corrio = False
        RelayFalso.instancias.append(self)

    async def ejecutar(self, parada: asyncio.Event) -> None:
        self.corrio = True
        await parada.wait()


@pytest.fixture
def relay(monkeypatch: pytest.MonkeyPatch) -> type[RelayFalso]:
    RelayFalso.instancias = []
    monkeypatch.setattr(main, "RelayOutbox", RelayFalso)
    monkeypatch.setattr(main, "cliente_sns", lambda region, endpoint_url=None: object())
    monkeypatch.setattr(main, "PublicadorSns", lambda cliente, topico: (cliente, topico))
    return RelayFalso


def habilitar(
    monkeypatch: pytest.MonkeyPatch, topico: str | None = "arn:aws:sns:us-east-1:0:eventos"
) -> None:
    monkeypatch.setenv("OUTBOX_ENABLED", "true")
    if topico:
        monkeypatch.setenv("EVENTS_TOPIC_ARN", topico)


def test_sin_outbox_habilitado_no_hay_relay(relay: type[RelayFalso]) -> None:
    with TestClient(crear_app(pool=PoolInerte())):  # type: ignore[arg-type]
        pass
    assert relay.instancias == []


def test_con_outbox_habilitado_el_relay_corre_y_se_detiene_al_cerrar(
    relay: type[RelayFalso], monkeypatch: pytest.MonkeyPatch
) -> None:
    habilitar(monkeypatch)
    monkeypatch.setenv("SOLVENTA_OUTBOX_LOTE", "7")
    app = crear_app(pool=PoolInerte())  # type: ignore[arg-type]
    with TestClient(app):
        tarea = app.state.relay_outbox
        assert relay.instancias[0].opciones["lote"] == 7
    assert tarea.done()
    assert relay.instancias[0].corrio


def test_sin_topico_lo_registra_y_la_app_arranca(
    relay: type[RelayFalso], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    habilitar(monkeypatch, None)
    with (
        caplog.at_level(logging.ERROR),
        TestClient(construir(caplog, pool=PoolInerte())) as cliente,  # type: ignore[arg-type]
    ):
        assert cliente.get("/health").status_code == 200
    assert relay.instancias == []
    assert any(
        r.getMessage() == main.MENSAJE_RELAY_SIN_CONFIGURACION and r.missing == "EVENTS_TOPIC_ARN"  # type: ignore[attr-defined]
        for r in caplog.records
    )


def test_sin_base_con_outbox_habilitado_lo_registra(
    relay: type[RelayFalso], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    habilitar(monkeypatch)
    with caplog.at_level(logging.ERROR), TestClient(construir(caplog)):
        pass
    assert any(getattr(r, "missing", None) == "base" for r in caplog.records)


def test_si_el_cliente_sns_falla_la_app_arranca_igual(
    relay: type[RelayFalso], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def falla(region: str, endpoint_url: str | None = None) -> object:
        raise RuntimeError("secreto en el mensaje")

    habilitar(monkeypatch)
    monkeypatch.setattr(main, "cliente_sns", falla)
    with (
        caplog.at_level(logging.ERROR),
        TestClient(construir(caplog, pool=PoolInerte())) as cliente,  # type: ignore[arg-type]
    ):
        assert cliente.get("/health").status_code == 200
    assert relay.instancias == []
    registro = next(r for r in caplog.records if r.getMessage() == main.MENSAJE_RELAY_NO_ARRANCO)
    assert registro.error_type == "RuntimeError"  # type: ignore[attr-defined]
    assert "secreto" not in registro.getMessage()


def test_un_error_del_relay_se_registra_y_la_app_sigue(
    relay: type[RelayFalso], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def revienta(self: RelayFalso, parada: asyncio.Event) -> None:
        raise ValueError("datos del cliente")

    monkeypatch.setattr(RelayFalso, "ejecutar", revienta)
    habilitar(monkeypatch)
    with (
        caplog.at_level(logging.ERROR),
        TestClient(construir(caplog, pool=PoolInerte())) as cliente,  # type: ignore[arg-type]
    ):
        assert cliente.get("/health").status_code == 200
    registro = next(r for r in caplog.records if r.getMessage() == main.MENSAJE_RELAY_DETENIDO)
    assert registro.error_type == "ValueError"  # type: ignore[attr-defined]


def test_un_relay_que_no_para_a_tiempo_se_cancela(
    relay: type[RelayFalso], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def no_para(self: RelayFalso, parada: asyncio.Event) -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr(RelayFalso, "ejecutar", no_para)
    monkeypatch.setattr(main, "PARADA_RELAY_S", 0.05)
    habilitar(monkeypatch)
    with (
        caplog.at_level(logging.WARNING),
        TestClient(construir(caplog, pool=PoolInerte())),  # type: ignore[arg-type]
    ):
        pass
    assert any(r.getMessage() == main.MENSAJE_RELAY_SIN_PARAR for r in caplog.records)


def test_el_openapi_se_arma_una_vez_y_publica_el_cuerpo_del_evento() -> None:
    app = crear_app()
    primero = app.openapi()
    assert app.openapi() is primero
    esquemas = primero["components"]["schemas"]
    assert {"EventoExterno", "Ubicacion", "RecepcionEvento", "ErrorDto"} <= set(esquemas)
    cuerpo = primero["paths"]["/v1/eventos"]["post"]["requestBody"]
    assert cuerpo["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/EventoExterno"
    }


def test_el_reloj_del_sistema_da_el_instante_utc() -> None:
    from adaptador_telemetria.adapters.outbound.reloj import RelojSistema

    ahora = RelojSistema().ahora()
    assert ahora.utcoffset() is not None and ahora.utcoffset().total_seconds() == 0  # type: ignore[union-attr]
