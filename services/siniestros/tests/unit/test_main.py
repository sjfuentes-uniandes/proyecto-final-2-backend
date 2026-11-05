"""Armado de la app: almacén, servicios, ciclo de vida y relay del Outbox (HU-W19)."""

import asyncio
import logging
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient
from siniestros import main
from siniestros.adapters.outbound.evidencias_s3 import AlmacenSinConfigurar, EvidenciasS3
from siniestros.application.siniestro.vistas import Plazos
from siniestros.config import Settings
from siniestros.main import armar_almacen, armar_servicios, crear_app


class PoolInerte:
    """Un pool que nadie usa: armar los servicios no abre conexiones."""

    def __init__(self) -> None:
        self.abierto = True


@pytest.fixture(autouse=True)
def credenciales(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")


def construir(caplog: pytest.LogCaptureFixture, *args: Any, **kwargs: Any) -> Any:
    """Crea la app y devuelve a caplog su handler: `instrumentar` reconfigura el logging raíz."""
    app = crear_app(*args, **kwargs)
    logging.getLogger().addHandler(caplog.handler)
    return app


def test_sin_bucket_el_almacen_no_verifica_ni_firma(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        almacen = armar_almacen(Settings())
    assert isinstance(almacen, AlmacenSinConfigurar)
    assert any("sin bucket" in r.getMessage() for r in caplog.records)


def test_con_bucket_el_almacen_es_s3_con_el_corte_configurado(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVIDENCIAS_BUCKET", "solventa-local-evidencias")
    monkeypatch.setenv("SOLVENTA_S3_TIMEOUT_MS", "250")
    almacen = armar_almacen(Settings())
    assert isinstance(almacen, EvidenciasS3)
    assert almacen._bucket == "solventa-local-evidencias"
    assert almacen._timeout_s == 0.25
    # Sin endpoint público se firma con el mismo cliente.
    assert almacen._firma is almacen._cliente


def test_con_endpoint_publico_las_urls_se_firman_con_otro_cliente(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVIDENCIAS_BUCKET", "solventa-local-evidencias")
    monkeypatch.setenv("SOLVENTA_AWS_ENDPOINT_URL", "http://localstack:4566")
    monkeypatch.setenv("SOLVENTA_EVIDENCIAS_ENDPOINT_PUBLICO", "http://localhost:4566")
    almacen = armar_almacen(Settings())
    assert isinstance(almacen, EvidenciasS3)
    assert almacen._cliente.meta.endpoint_url == "http://localstack:4566"
    assert almacen._firma.meta.endpoint_url == "http://localhost:4566"


def test_los_servicios_usan_los_plazos_de_la_configuracion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOLVENTA_COMPROMISO_HORAS", "24")
    monkeypatch.setenv("SOLVENTA_PRIORIDAD_ALTA_MIN", "120")
    monkeypatch.setenv("SOLVENTA_PRIORIDAD_MEDIA_MIN", "600")
    servicios = armar_servicios(
        PoolInerte(), AlmacenSinConfigurar(), main.RelojSistema(), Settings()
    )  # type: ignore[arg-type]
    assert servicios.plazos == Plazos(24, 120, 600)


def test_con_un_pool_inyectado_la_app_arma_los_servicios_y_no_migra() -> None:
    app = crear_app(pool=PoolInerte())  # type: ignore[arg-type]
    with TestClient(app) as cliente:
        assert cliente.get("/health").json() == {"status": "ok"}
        assert app.state.servicios is not None
        assert not hasattr(app.state, "relay_outbox")


def test_con_base_configurada_migra_abre_el_pool_y_lo_cierra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eventos: list[str] = []

    class PoolFalso:
        def __init__(self, url: str, **kwargs: Any) -> None:
            eventos.append(f"pool:{kwargs['min_size']}-{kwargs['max_size']}")

        async def open(self) -> None:
            eventos.append("abre")

        async def close(self) -> None:
            eventos.append("cierra")

    import psycopg_pool

    monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", PoolFalso)
    monkeypatch.setattr(
        main, "migrar", lambda url: eventos.append(f"migra:{url.split('/')[-1].split('?')[0]}")
    )
    monkeypatch.setenv("SOLVENTA_DB_HOST", "rds.interno")
    monkeypatch.setenv("SOLVENTA_DB_POOL_SIZE", "7")
    with TestClient(crear_app(Settings())) as cliente:
        assert cliente.get("/health").status_code == 200
        assert eventos == ["migra:siniestros", "pool:1-7", "abre"]
    assert eventos[-1] == "cierra"


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


@pytest.mark.parametrize(("topico", "falta"), [(None, "EVENTS_TOPIC_ARN")])
def test_sin_topico_lo_registra_y_la_app_arranca(
    relay: type[RelayFalso],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    topico: str | None,
    falta: str,
) -> None:
    habilitar(monkeypatch, topico)
    with (
        caplog.at_level(logging.ERROR),
        TestClient(construir(caplog, pool=PoolInerte())) as cliente,
    ):  # type: ignore[arg-type]
        assert cliente.get("/health").status_code == 200
    assert relay.instancias == []
    assert any(
        r.getMessage() == main.MENSAJE_RELAY_SIN_CONFIGURACION and r.missing == falta
        for r in caplog.records
    )  # type: ignore[attr-defined]


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
        TestClient(construir(caplog, pool=PoolInerte())) as cliente,
    ):  # type: ignore[arg-type]
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
        TestClient(construir(caplog, pool=PoolInerte())) as cliente,
    ):  # type: ignore[arg-type]
        assert cliente.get("/health").status_code == 200
    registro = next(r for r in caplog.records if r.getMessage() == main.MENSAJE_RELAY_DETENIDO)
    assert registro.error_type == "ValueError"  # type: ignore[attr-defined]


def test_si_el_relay_no_se_detiene_a_tiempo_se_cancela(
    relay: type[RelayFalso], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def sordo(self: RelayFalso, parada: asyncio.Event) -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr(RelayFalso, "ejecutar", sordo)
    monkeypatch.setattr(main, "PARADA_RELAY_S", 0.05)
    habilitar(monkeypatch)
    with caplog.at_level(logging.WARNING), TestClient(construir(caplog, pool=PoolInerte())):  # type: ignore[arg-type]
        pass
    assert any(r.getMessage() == main.MENSAJE_RELAY_SIN_PARAR for r in caplog.records)
