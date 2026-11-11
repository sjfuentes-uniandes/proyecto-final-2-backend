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


# --- Semilla de QA ---------------------------------------------------------------------------------
@pytest.fixture
def semilla(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    llamadas: list[Any] = []

    async def falsa(
        parada: asyncio.Event,
        pool: Any,
        cliente: Any,
        bucket: Any,
        sse: Any,
        reloj: Any,
        analista: Any,
    ) -> None:
        llamadas.append((pool, cliente, bucket, sse, analista))
        await parada.wait()

    monkeypatch.setattr(main, "sembrar_con_reintentos", falsa)
    return llamadas


def test_la_semilla_esta_apagada_por_omision(semilla: list[Any]) -> None:
    with TestClient(crear_app(pool=PoolInerte())):  # type: ignore[arg-type]
        pass
    assert semilla == []


def test_encendida_siembra_con_el_bucket_el_cifrado_y_el_analista(
    semilla: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOLVENTA_SEMILLA_SINIESTROS_QA", "true")
    monkeypatch.setenv("EVIDENCIAS_BUCKET", "solventa-local-evidencias")
    monkeypatch.setenv("EVIDENCIAS_BUCKET_SSE", "aws:kms")
    monkeypatch.setenv(
        "SOLVENTA_SEMILLA_SINIESTROS_ANALISTA_ID", "b0b0b0b0-0000-4000-8000-000000000009"
    )
    pool = PoolInerte()
    app = crear_app(pool=pool)  # type: ignore[arg-type]
    with TestClient(app):
        tarea = app.state.semilla_qa
    ((pool_usado, cliente, bucket, sse, analista),) = semilla
    assert pool_usado is pool
    assert cliente is not None
    assert (bucket, sse, str(analista)) == (
        "solventa-local-evidencias",
        "aws:kms",
        "b0b0b0b0-0000-4000-8000-000000000009",
    )
    assert tarea.done()


def test_sin_bucket_siembra_solo_la_base(
    semilla: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOLVENTA_SEMILLA_SINIESTROS_QA", "true")
    with TestClient(crear_app(pool=PoolInerte())):  # type: ignore[arg-type]
        pass
    assert semilla[0][1:3] == (None, None)


@pytest.mark.parametrize("ambiente", ["prod", "production", "PROD"])
def test_en_produccion_no_se_siembra_aunque_este_encendida(
    semilla: list[Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    ambiente: str,
) -> None:
    monkeypatch.setenv("SOLVENTA_SEMILLA_SINIESTROS_QA", "true")
    monkeypatch.setenv("SOLVENTA_ENVIRONMENT", ambiente)
    with caplog.at_level(logging.ERROR), TestClient(construir(caplog, pool=PoolInerte())):  # type: ignore[arg-type]
        pass
    assert semilla == []
    assert any(r.getMessage() == main.MENSAJE_SEMILLA_EN_PRODUCCION for r in caplog.records)


def test_sin_base_la_semilla_no_corre(
    semilla: list[Any], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("SOLVENTA_SEMILLA_SINIESTROS_QA", "true")
    with caplog.at_level(logging.ERROR), TestClient(construir(caplog)):
        pass
    assert semilla == []
    assert any(getattr(r, "missing", None) == "base" for r in caplog.records)


def test_un_error_de_la_semilla_no_tumba_la_app(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def revienta(*argumentos: Any) -> None:
        raise ValueError("datos")

    monkeypatch.setattr(main, "sembrar_con_reintentos", revienta)
    monkeypatch.setenv("SOLVENTA_SEMILLA_SINIESTROS_QA", "true")
    with (
        caplog.at_level(logging.ERROR),
        TestClient(construir(caplog, pool=PoolInerte())) as cliente,
    ):  # type: ignore[arg-type]
        assert cliente.get("/health").status_code == 200
    assert any(r.getMessage() == main.MENSAJE_SEMILLA_DETENIDA for r in caplog.records)


# --- Tarea de plazos (HU-W20, DS3-13) ----------------------------------------------------------------
class PlazosFalsos:
    instancias: ClassVar[list["PlazosFalsos"]] = []

    def __init__(self, repo: Any, reloj: Any, lote: int) -> None:
        self.lote = lote
        self.intervalo_s: float | None = None
        self.corrio = False
        PlazosFalsos.instancias.append(self)

    async def ejecutar(self, parada: asyncio.Event, intervalo_s: float) -> None:
        self.corrio = True
        self.intervalo_s = intervalo_s
        await parada.wait()


@pytest.fixture
def plazos(monkeypatch: pytest.MonkeyPatch) -> type[PlazosFalsos]:
    PlazosFalsos.instancias = []
    monkeypatch.setattr(main, "VencerPlazosSoporte", PlazosFalsos)
    return PlazosFalsos


def test_la_tarea_de_plazos_corre_con_la_configuracion_y_se_detiene_al_cerrar(
    plazos: type[PlazosFalsos], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOLVENTA_PLAZOS_INTERVALO_S", "15")
    monkeypatch.setenv("SOLVENTA_PLAZOS_LOTE", "7")
    app = crear_app(pool=PoolInerte())  # type: ignore[arg-type]
    with TestClient(app):
        tarea = app.state.plazos_soporte
        assert not tarea.done()
    assert tarea.done()
    (instancia,) = plazos.instancias
    assert (instancia.lote, instancia.intervalo_s, instancia.corrio) == (7, 15.0, True)


def test_por_omision_la_tarea_de_plazos_vence_cada_60_segundos_en_lotes_de_50(
    plazos: type[PlazosFalsos],
) -> None:
    with TestClient(crear_app(pool=PoolInerte())):  # type: ignore[arg-type]
        pass
    assert (plazos.instancias[0].lote, plazos.instancias[0].intervalo_s) == (50, 60.0)


def test_la_tarea_de_plazos_se_puede_apagar(
    plazos: type[PlazosFalsos], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOLVENTA_PLAZOS_HABILITADOS", "false")
    app = crear_app(pool=PoolInerte())  # type: ignore[arg-type]
    with TestClient(app):
        assert not hasattr(app.state, "plazos_soporte")
    assert plazos.instancias == []


def test_sin_base_la_tarea_de_plazos_no_corre_y_la_app_arranca(
    plazos: type[PlazosFalsos], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING), TestClient(construir(caplog)) as cliente:
        assert cliente.get("/health").status_code == 200
    assert plazos.instancias == []
    assert any(r.getMessage() == main.MENSAJE_PLAZOS_SIN_BASE for r in caplog.records)


def test_si_la_tarea_de_plazos_no_se_detiene_a_tiempo_se_cancela(
    plazos: type[PlazosFalsos], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def sorda(self: PlazosFalsos, parada: asyncio.Event, intervalo_s: float) -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr(PlazosFalsos, "ejecutar", sorda)
    monkeypatch.setattr(main, "PARADA_PLAZOS_S", 0.05)
    with caplog.at_level(logging.WARNING), TestClient(construir(caplog, pool=PoolInerte())):  # type: ignore[arg-type]
        pass
    assert any(r.getMessage() == main.MENSAJE_PLAZOS_SIN_PARAR for r in caplog.records)


def test_un_error_de_la_tarea_de_plazos_se_registra_y_la_app_sigue(
    plazos: type[PlazosFalsos], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def rota(self: PlazosFalsos, parada: asyncio.Event, intervalo_s: float) -> None:
        raise ValueError("DATO-PRIVADO")

    monkeypatch.setattr(PlazosFalsos, "ejecutar", rota)
    with (
        caplog.at_level(logging.ERROR),
        TestClient(construir(caplog, pool=PoolInerte())) as cliente,
    ):  # type: ignore[arg-type]
        assert cliente.get("/health").status_code == 200
    registro = next(r for r in caplog.records if r.getMessage() == main.MENSAJE_PLAZOS_DETENIDOS)
    assert registro.error_type == "ValueError"  # type: ignore[attr-defined]
    assert "DATO-PRIVADO" not in caplog.text


def test_la_tarea_de_plazos_se_detiene_antes_que_el_relay_para_que_este_publique_lo_ultimo(
    plazos: type[PlazosFalsos], relay: type[RelayFalso], monkeypatch: pytest.MonkeyPatch
) -> None:
    orden: list[str] = []

    async def plazos_cierra(self: PlazosFalsos, parada: asyncio.Event, intervalo_s: float) -> None:
        await parada.wait()
        orden.append("plazos")

    async def relay_cierra(self: RelayFalso, parada: asyncio.Event) -> None:
        await parada.wait()
        orden.append("relay")

    monkeypatch.setattr(PlazosFalsos, "ejecutar", plazos_cierra)
    monkeypatch.setattr(RelayFalso, "ejecutar", relay_cierra)
    habilitar(monkeypatch)
    with TestClient(crear_app(pool=PoolInerte())):  # type: ignore[arg-type]
        pass
    assert orden == ["plazos", "relay"]


# --- Consumidor de siniestros-indemnizaciones (HU-W23, DS3-25) -----------------------------------------
COLA = "https://sqs.us-east-1.amazonaws.com/000000000000/solventa-int-siniestros-indemnizaciones"


class ConsumidorFalso:
    instancias: ClassVar[list["ConsumidorFalso"]] = []

    def __init__(self, receptor: Any, repositorio: Any, manejador: Any, **opciones: Any) -> None:
        self.argumentos = (receptor, repositorio, manejador)
        self.opciones = opciones
        self.corrio = False
        ConsumidorFalso.instancias.append(self)

    async def ejecutar(self, parada: asyncio.Event) -> None:
        self.corrio = True
        await parada.wait()


@pytest.fixture
def consumidor(monkeypatch: pytest.MonkeyPatch) -> type[ConsumidorFalso]:
    ConsumidorFalso.instancias = []
    llamadas: list[Any] = []

    def sqs(region: str, *, endpoint_url: str | None = None, espera_larga_s: int) -> Any:
        llamadas.append((region, endpoint_url, espera_larga_s))
        from types import SimpleNamespace

        from solventa_common import inbox

        return SimpleNamespace(
            meta=SimpleNamespace(
                config=SimpleNamespace(read_timeout=espera_larga_s + inbox.MARGEN_LECTURA_S)
            )
        )

    monkeypatch.setattr(main, "ConsumidorInbox", ConsumidorFalso)
    monkeypatch.setattr(main, "cliente_sqs", sqs)
    ConsumidorFalso.llamadas_sqs = llamadas  # type: ignore[attr-defined]
    return ConsumidorFalso


def test_sin_cola_no_hay_consumidor_y_se_avisa(
    consumidor: type[ConsumidorFalso], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING), TestClient(construir(caplog, pool=PoolInerte())) as c:  # type: ignore[arg-type]
        assert c.get("/health").status_code == 200
    assert consumidor.instancias == []
    aviso = next(r for r in caplog.records if r.getMessage() == main.MENSAJE_CONSUMIDOR_SIN_COLA)
    assert aviso.missing == "SQS_SINIESTROS_INDEMNIZACIONES_URL"  # type: ignore[attr-defined]


def test_con_cola_el_consumidor_corre_con_lote_5_y_espera_10_y_se_detiene_al_cerrar(
    consumidor: type[ConsumidorFalso], monkeypatch: pytest.MonkeyPatch
) -> None:
    from solventa_common import inbox

    monkeypatch.setenv("SQS_SINIESTROS_INDEMNIZACIONES_URL", COLA)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localstack:4566")
    pool = PoolInerte()
    app = crear_app(pool=pool)  # type: ignore[arg-type]
    with TestClient(app):
        tarea = app.state.consumidor_indemnizaciones
        assert not tarea.done()
    assert tarea.done() and not tarea.cancelled()
    [instancia] = consumidor.instancias
    receptor, repositorio, manejador = instancia.argumentos
    assert isinstance(receptor, inbox.ReceptorSqs)
    assert (receptor.max_mensajes, receptor.espera_larga_s) == (5, 10)
    assert isinstance(repositorio, inbox.InboxPg) and repositorio._pool is pool
    assert callable(manejador) and instancia.corrio
    assert instancia.opciones["reloj"]().utcoffset() is not None
    assert consumidor.llamadas_sqs == [("us-east-1", "http://localstack:4566", 10)]  # type: ignore[attr-defined]


def test_el_manejador_abre_la_vista_de_siniestros_sobre_la_transaccion_del_inbox(
    consumidor: type[ConsumidorFalso], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Modo (a): el resultado del pago y la fila del inbox comparten la conexión de la transacción."""
    from types import SimpleNamespace

    from siniestros.adapters.outbound.siniestros_pg import TxSiniestrosPg

    capturado: list[Any] = []

    class CasoFalso:
        async def registrar(self, resultado: Any, tx: Any) -> None:
            capturado.append(tx)

    monkeypatch.setattr(main, "RegistrarResultadoPago", CasoFalso)
    monkeypatch.setenv("SQS_SINIESTROS_INDEMNIZACIONES_URL", COLA)
    with TestClient(crear_app(pool=PoolInerte())):  # type: ignore[arg-type]
        pass
    [instancia] = consumidor.instancias
    manejador = instancia.argumentos[2]
    conexion = object()
    import json
    from datetime import UTC, datetime
    from uuid import uuid4

    from solventa_common.inbox import EventoRecibido

    cuerpo = json.dumps(
        {
            "eventId": str(uuid4()),
            "aggregateId": str(uuid4()),
            "type": "IndemnizacionConfirmada",
            "version": 1,
            "occurredAt": datetime.now(UTC).isoformat(),
            "correlationId": "c",
            "payload": {
                "siniestroId": str(uuid4()),
                "referencia": "IND-88-2026-0442-01",
                "transaccionId": "T",
                "intentos": 1,
                "duplicadosDescartados": 0,
                "confirmadaEn": datetime.now(UTC).isoformat(),
            },
        }
    )
    evento = EventoRecibido(
        event_id=uuid4(),
        event_type="IndemnizacionConfirmada",
        correlation_id="c",
        sobre={},
        cuerpo=cuerpo,
        message_id="m",
        recepciones=1,
    )
    asyncio.run(manejador(evento, SimpleNamespace(conexion=conexion)))
    [tx] = capturado
    assert isinstance(tx, TxSiniestrosPg) and tx._bruta is conexion


def test_con_cola_y_sin_base_arranca_y_lo_registra(
    consumidor: type[ConsumidorFalso],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("SQS_SINIESTROS_INDEMNIZACIONES_URL", COLA)
    with caplog.at_level(logging.ERROR), TestClient(construir(caplog)) as cliente:
        assert cliente.get("/health").status_code == 200
    assert consumidor.instancias == []
    assert any(r.getMessage() == main.MENSAJE_CONSUMIDOR_SIN_BASE for r in caplog.records)


def test_si_no_se_puede_crear_el_cliente_sqs_arranca_y_lo_registra(
    consumidor: type[ConsumidorFalso],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def sqs(region: str, *, endpoint_url: str | None = None, espera_larga_s: int) -> Any:
        raise ValueError(f"Invalid endpoint: {endpoint_url}")

    monkeypatch.setattr(main, "cliente_sqs", sqs)
    monkeypatch.setenv("SQS_SINIESTROS_INDEMNIZACIONES_URL", COLA)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "no-url")
    with caplog.at_level(logging.ERROR), TestClient(construir(caplog, pool=PoolInerte())) as c:  # type: ignore[arg-type]
        assert c.get("/health").status_code == 200
    registro = next(
        r for r in caplog.records if r.getMessage() == main.MENSAJE_CONSUMIDOR_NO_ARRANCO
    )
    assert registro.error_type == "ValueError"  # type: ignore[attr-defined]
    assert "no-url" not in caplog.text


def test_un_consumidor_que_falla_no_tumba_la_app(
    consumidor: type[ConsumidorFalso],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def rota(self: ConsumidorFalso, parada: asyncio.Event) -> None:
        raise ValueError("DATO-PRIVADO")

    monkeypatch.setattr(ConsumidorFalso, "ejecutar", rota)
    monkeypatch.setenv("SQS_SINIESTROS_INDEMNIZACIONES_URL", COLA)
    with caplog.at_level(logging.ERROR), TestClient(construir(caplog, pool=PoolInerte())) as c:  # type: ignore[arg-type]
        assert c.get("/health").status_code == 200
    registro = next(r for r in caplog.records if r.getMessage() == main.MENSAJE_CONSUMIDOR_DETENIDO)
    assert registro.error_type == "ValueError"  # type: ignore[attr-defined]
    assert "DATO-PRIVADO" not in caplog.text


def test_si_el_consumidor_no_se_detiene_a_tiempo_se_cancela(
    consumidor: type[ConsumidorFalso],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def sorda(self: ConsumidorFalso, parada: asyncio.Event) -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr(ConsumidorFalso, "ejecutar", sorda)
    monkeypatch.setattr(main, "PARADA_CONSUMIDOR_S", 0.05)
    monkeypatch.setenv("SQS_SINIESTROS_INDEMNIZACIONES_URL", COLA)
    with caplog.at_level(logging.WARNING), TestClient(construir(caplog, pool=PoolInerte())):  # type: ignore[arg-type]
        pass
    assert any(r.getMessage() == main.MENSAJE_CONSUMIDOR_SIN_PARAR for r in caplog.records)


def test_el_consumidor_y_las_tareas_paran_a_la_vez_y_antes_que_el_relay(
    consumidor: type[ConsumidorFalso],
    plazos: type[PlazosFalsos],
    relay: type[RelayFalso],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orden: list[str] = []

    async def consumidor_cierra(self: ConsumidorFalso, parada: asyncio.Event) -> None:
        await parada.wait()
        orden.append("consumidor")

    async def plazos_cierra(self: PlazosFalsos, parada: asyncio.Event, intervalo_s: float) -> None:
        await parada.wait()
        orden.append("plazos")

    async def relay_cierra(self: RelayFalso, parada: asyncio.Event) -> None:
        await parada.wait()
        orden.append("relay")

    monkeypatch.setattr(ConsumidorFalso, "ejecutar", consumidor_cierra)
    monkeypatch.setattr(PlazosFalsos, "ejecutar", plazos_cierra)
    monkeypatch.setattr(RelayFalso, "ejecutar", relay_cierra)
    monkeypatch.setenv("SQS_SINIESTROS_INDEMNIZACIONES_URL", COLA)
    habilitar(monkeypatch)
    with TestClient(crear_app(pool=PoolInerte())):  # type: ignore[arg-type]
        pass
    assert orden[-1] == "relay" and sorted(orden[:2]) == ["consumidor", "plazos"]


def test_por_omision_el_consumidor_usa_lote_5_y_espera_10() -> None:
    settings = Settings()
    assert (settings.consumidor_max_mensajes, settings.consumidor_espera_larga_s) == (5, 10)
    for campo in (
        {"consumidor_max_mensajes": 0},
        {"consumidor_max_mensajes": 11},
        {"consumidor_espera_larga_s": 11},
    ):
        with pytest.raises(ValueError):
            Settings(**campo)
