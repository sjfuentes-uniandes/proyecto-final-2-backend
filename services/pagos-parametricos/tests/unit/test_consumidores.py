"""El cableado de los consumidores de HU-W21 en `main.py`: la forma obligatoria de HU-W24 (§9), el
pool dimensionado, el arranque y la parada ordenada (consumidores antes que el relay, R-77)."""

import asyncio
import logging
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient
from pagos_parametricos import main
from pagos_parametricos.adapters.outbound.descartes_pg import RegistroFallosPg
from pagos_parametricos.config import Settings
from pagos_parametricos.main import armar_consumidores, crear_app
from solventa_common.inbox import ConsumidorInbox

EVENTOS = "http://localstack:4566/000000000000/solventa-local-parametricos-eventos"
POLIZAS = "http://localstack:4566/000000000000/solventa-local-parametricos-polizas"


class PoolInerte:
    """Un pool que nadie usa: armar los consumidores no abre conexiones."""


@pytest.fixture(autouse=True)
def credenciales(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")


def _cliente() -> Any:
    from solventa_common.inbox import cliente_sqs

    return cliente_sqs("us-east-1", endpoint_url="http://localhost:4566")


# --- Configuración -------------------------------------------------------------------------------
def test_la_concurrencia_por_omision_es_10_y_se_configura_con_sqs_concurrencia(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert Settings().sqs_concurrencia == 10
    monkeypatch.setenv("SQS_CONCURRENCIA", "4")
    assert Settings().sqs_concurrencia == 4


@pytest.mark.parametrize("valor", ["0", "101", "-1"])
def test_una_concurrencia_fuera_de_rango_se_rechaza(
    monkeypatch: pytest.MonkeyPatch, valor: str
) -> None:
    monkeypatch.setenv("SQS_CONCURRENCIA", valor)
    with pytest.raises(ValueError):
        Settings()


@pytest.mark.parametrize(
    ("concurrencia", "pool", "esperado"),
    [(10, 5, 23), (10, 50, 50), (1, 5, 5), (1, 2, 5), (100, 5, 203)],
)
def test_el_pool_admite_la_concurrencia_de_los_dos_consumidores_y_solo_puede_subir(
    monkeypatch: pytest.MonkeyPatch, concurrencia: int, pool: int, esperado: int
) -> None:
    monkeypatch.setenv("SQS_CONCURRENCIA", str(concurrencia))
    monkeypatch.setenv("DB_POOL_SIZE", str(pool))
    assert Settings().pool_max == esperado


# --- La forma obligatoria (W24 §9) ---------------------------------------------------------------
def test_ambos_consumidores_con_contrapresion_latido_y_parada(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SQS_CONCURRENCIA", "7")
    monkeypatch.setenv("SQS_PARAMETRICOS_EVENTOS_URL", EVENTOS)
    monkeypatch.setenv("SQS_PARAMETRICOS_POLIZAS_URL", POLIZAS)
    consumidores = armar_consumidores(PoolInerte(), _cliente(), main.RelojSistema(), Settings())  # type: ignore[arg-type]
    assert set(consumidores) == {"consumidor_eventos", "consumidor_polizas"}
    for consumidor in consumidores.values():
        assert isinstance(consumidor, ConsumidorInbox)
        assert consumidor._concurrencia == 7
        assert consumidor._visibilidad_s == 30
        assert consumidor._plazo_parada_s == 10
        assert consumidor._receptor.max_mensajes == 10  # type: ignore[attr-defined]
    eventos, polizas = consumidores["consumidor_eventos"], consumidores["consumidor_polizas"]
    assert eventos._receptor._cola_url == EVENTOS  # type: ignore[attr-defined]
    assert polizas._receptor._cola_url == POLIZAS  # type: ignore[attr-defined]
    # Solo `parametricos-eventos` anota la causa del fallo, que luego muestra H10.
    assert getattr(eventos._al_fallar, "__func__", None) is RegistroFallosPg.al_fallar
    assert polizas._al_fallar is None


def test_solo_se_arman_los_consumidores_de_las_colas_configuradas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SQS_PARAMETRICOS_POLIZAS_URL", POLIZAS)
    consumidores = armar_consumidores(PoolInerte(), _cliente(), main.RelojSistema(), Settings())  # type: ignore[arg-type]
    assert set(consumidores) == {"consumidor_polizas"}
    assert (
        armar_consumidores(
            PoolInerte(),
            _cliente(),
            main.RelojSistema(),
            Settings(SQS_PARAMETRICOS_POLIZAS_URL=None),
        )
        == {}
    )  # type: ignore[arg-type]


# --- Ciclo de vida -------------------------------------------------------------------------------
class ConsumidorFalso:
    instancias: ClassVar[list["ConsumidorFalso"]] = []
    bitacora: ClassVar[list[str]] = []

    def __init__(self, receptor: Any, repositorio: Any, manejador: Any, **opciones: Any) -> None:
        self.cola = receptor._cola_url
        self.opciones = opciones
        self.corrio = False
        ConsumidorFalso.instancias.append(self)

    async def ejecutar(self, parada: asyncio.Event) -> None:
        self.corrio = True
        ConsumidorFalso.bitacora.append(f"arranca:{self.cola.rsplit('-', 1)[-1]}")
        await parada.wait()
        ConsumidorFalso.bitacora.append(f"para:{self.cola.rsplit('-', 1)[-1]}")


@pytest.fixture
def falsos(monkeypatch: pytest.MonkeyPatch) -> type[ConsumidorFalso]:
    ConsumidorFalso.instancias = []
    ConsumidorFalso.bitacora = []
    monkeypatch.setattr(main, "ConsumidorInbox", ConsumidorFalso)
    return ConsumidorFalso


def construir(caplog: pytest.LogCaptureFixture, *args: Any, **kwargs: Any) -> Any:
    app = crear_app(*args, **kwargs)
    logging.getLogger().addHandler(caplog.handler)
    return app


def test_los_consumidores_corren_con_la_app_y_se_detienen_al_cerrar(
    falsos: type[ConsumidorFalso], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SQS_PARAMETRICOS_EVENTOS_URL", EVENTOS)
    monkeypatch.setenv("SQS_PARAMETRICOS_POLIZAS_URL", POLIZAS)
    app = crear_app(pool=PoolInerte())  # type: ignore[arg-type]
    with TestClient(app):
        tareas = [app.state.consumidor_eventos, app.state.consumidor_polizas]
        assert len(falsos.instancias) == 2
        assert all(not t.done() for t in tareas)
    assert all(t.done() for t in tareas)
    assert all(c.corrio for c in falsos.instancias)
    assert sorted(falsos.bitacora) == sorted(
        ["arranca:eventos", "arranca:polizas", "para:eventos", "para:polizas"]
    )


def test_los_consumidores_paran_antes_que_el_relay_para_que_este_publique_lo_ultimo(
    falsos: type[ConsumidorFalso], monkeypatch: pytest.MonkeyPatch
) -> None:
    class RelayFalso:
        def __init__(self, *args: Any, **kwargs: Any) -> None: ...

        async def ejecutar(self, parada: asyncio.Event) -> None:
            await parada.wait()
            ConsumidorFalso.bitacora.append("para:relay")

    monkeypatch.setattr(main, "RelayOutbox", RelayFalso)
    monkeypatch.setattr(main, "cliente_sns", lambda region, endpoint_url=None: object())
    monkeypatch.setattr(main, "PublicadorSns", lambda cliente, topico: (cliente, topico))
    monkeypatch.setenv("OUTBOX_ENABLED", "true")
    monkeypatch.setenv("EVENTS_TOPIC_ARN", "arn:aws:sns:us-east-1:0:eventos")
    monkeypatch.setenv("SQS_PARAMETRICOS_EVENTOS_URL", EVENTOS)
    monkeypatch.setenv("SQS_PARAMETRICOS_POLIZAS_URL", POLIZAS)
    with TestClient(crear_app(pool=PoolInerte())):  # type: ignore[arg-type]
        pass
    paradas = [e for e in falsos.bitacora if e.startswith("para:")]
    assert paradas[-1] == "para:relay"
    assert set(paradas[:-1]) == {"para:eventos", "para:polizas"}


def test_sin_base_o_sin_colas_no_hay_consumidores(
    falsos: type[ConsumidorFalso], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO), TestClient(construir(caplog, pool=PoolInerte())):  # type: ignore[arg-type]
        pass
    assert falsos.instancias == []
    assert main.MENSAJE_CONSUMO_SIN_COLA in [r.getMessage() for r in caplog.records]
    monkeypatch.setenv("SQS_PARAMETRICOS_EVENTOS_URL", EVENTOS)
    caplog.clear()
    with caplog.at_level(logging.INFO), TestClient(construir(caplog)):
        pass
    assert falsos.instancias == []
    assert main.MENSAJE_CONSUMO_SIN_COLA in [r.getMessage() for r in caplog.records]


def test_si_el_cliente_sqs_falla_la_app_arranca_igual(
    falsos: type[ConsumidorFalso],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def falla(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("secreto en el mensaje")

    monkeypatch.setattr(main, "cliente_sqs", falla)
    monkeypatch.setenv("SQS_PARAMETRICOS_EVENTOS_URL", EVENTOS)
    with caplog.at_level(logging.ERROR), TestClient(construir(caplog, pool=PoolInerte())):  # type: ignore[arg-type]
        pass  # arranca y cierra sin consumidores
    assert falsos.instancias == []
    registro = next(r for r in caplog.records if r.getMessage() == main.MENSAJE_CONSUMO_NO_ARRANCO)
    assert registro.error_type == "RuntimeError"  # type: ignore[attr-defined]
    assert "secreto" not in registro.getMessage()


def test_un_consumidor_que_no_para_a_tiempo_se_cancela(
    falsos: type[ConsumidorFalso],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def no_para(self: ConsumidorFalso, parada: asyncio.Event) -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr(ConsumidorFalso, "ejecutar", no_para)
    monkeypatch.setattr(main, "PARADA_CONSUMIDOR_S", 0.05)
    monkeypatch.setenv("SQS_PARAMETRICOS_EVENTOS_URL", EVENTOS)
    with caplog.at_level(logging.WARNING), TestClient(construir(caplog, pool=PoolInerte())):  # type: ignore[arg-type]
        pass
    assert any(r.getMessage() == main.MENSAJE_CONSUMO_SIN_PARAR for r in caplog.records)


def test_con_base_el_pool_se_abre_con_el_tamano_dimensionado(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import psycopg_pool

    creados: list[dict[str, Any]] = []

    class PoolFalso:
        def __init__(self, url: str, **argumentos: Any) -> None:
            creados.append(argumentos)

        async def open(self) -> None: ...

        async def close(self) -> None: ...

    monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", PoolFalso)
    monkeypatch.setattr(main, "migrar", lambda url: None)
    monkeypatch.setenv("SOLVENTA_DB_HOST", "rds.interno")
    monkeypatch.setenv("SQS_CONCURRENCIA", "10")
    with TestClient(crear_app(Settings())):
        pass
    assert creados[0]["max_size"] == 23 and creados[0]["min_size"] == 1
