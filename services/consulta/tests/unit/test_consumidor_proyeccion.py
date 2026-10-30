"""Configuración y ciclo de vida del consumidor de `consulta-proyeccion` (HU-M10).

La infraestructura entrega `SQS_CONSULTA_PROYECCION_URL` a consulta (`consumes`, ecs.tf:82) y
`AWS_REGION` a todos. Sin la cola (local y pruebas) el consumidor no arranca; con ella, corre como
tarea del ciclo de vida sobre el pool. Si falta la base o el cliente SQS no se puede crear, la
aplicación sigue sirviendo.
"""

import asyncio
from typing import Any, ClassVar, Self

import pytest
from consulta import main
from consulta.config import Settings
from consulta.main import crear_app
from fastapi.testclient import TestClient
from solventa_common import inbox

COLA = "https://sqs.us-east-1.amazonaws.com/000000000000/solventa-int-consulta-proyeccion"
_NOMBRES = ("SQS_CONSULTA_PROYECCION_URL", "AWS_REGION", "AWS_ENDPOINT_URL", "CATALOGO_URL")


@pytest.fixture(autouse=True)
def sin_entorno(monkeypatch: pytest.MonkeyPatch) -> None:
    for nombre in _NOMBRES:
        monkeypatch.delenv(nombre, raising=False)
        monkeypatch.delenv(f"SOLVENTA_{nombre}", raising=False)


def test_por_omision_sin_cola_lote_de_la_libreria_y_espera_de_10_s() -> None:
    settings = Settings()
    assert settings.sqs_consulta_proyeccion_url is None
    assert settings.consumidor_max_mensajes == inbox.MAX_MENSAJES
    assert settings.consumidor_espera_larga_s == 10


def test_la_cola_como_la_pone_ecs_y_su_variante_solventa(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SQS_CONSULTA_PROYECCION_URL", COLA)
    assert Settings().sqs_consulta_proyeccion_url == COLA
    monkeypatch.delenv("SQS_CONSULTA_PROYECCION_URL")
    monkeypatch.setenv("SOLVENTA_SQS_CONSULTA_PROYECCION_URL", COLA)
    assert Settings().sqs_consulta_proyeccion_url == COLA


@pytest.mark.parametrize(
    "campo",
    [
        {"consumidor_max_mensajes": 0},
        {"consumidor_max_mensajes": 11},
        {"consumidor_espera_larga_s": 0},
        {"consumidor_espera_larga_s": 11},
        {"catalogo_timeout_ms": 0},
        {"catalogo_timeout_ms": 701},
    ],
)
def test_lote_espera_y_timeout_acotados(campo: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Settings(**campo)


def test_la_parada_del_consumidor_cabe_en_el_stop_timeout_de_ecs() -> None:
    assert main.PARADA_CONSUMIDOR_S > Settings().consumidor_espera_larga_s
    assert main.PARADA_CONSUMIDOR_S <= 25


class PoolFalso:
    def connection(self) -> Any:
        class Conexion:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *_: object) -> None:
                return None

            async def execute(self, sql: str) -> None:
                return None

        return Conexion()


class PoolCaido:
    def connection(self) -> Any:
        raise OSError("sin base")


class ConsumidorFalso:
    creados: ClassVar[list["ConsumidorFalso"]] = []

    def __init__(self, receptor: Any, repositorio: Any, manejador: Any, **opciones: Any) -> None:
        self.receptor = receptor
        self.parado = False
        ConsumidorFalso.creados.append(self)

    async def ejecutar(self, parada: asyncio.Event) -> None:
        await parada.wait()
        self.parado = True


@pytest.fixture
def consumidor_falso(monkeypatch: pytest.MonkeyPatch) -> type[ConsumidorFalso]:
    ConsumidorFalso.creados = []
    monkeypatch.setattr(main, "ConsumidorInbox", ConsumidorFalso)
    monkeypatch.setattr(main, "cliente_sqs", lambda *a, **k: object())
    monkeypatch.setattr(main, "ReceptorSqs", lambda *a, **k: object())
    return ConsumidorFalso


def test_sin_cola_no_arranca_el_consumidor(consumidor_falso: type[ConsumidorFalso]) -> None:
    with TestClient(crear_app(Settings(), pool=PoolFalso())) as cliente:  # type: ignore[arg-type]
        assert cliente.get("/health").json() == {"status": "ok"}
        assert not hasattr(cliente.app.state, "consumidor_proyeccion")  # type: ignore[attr-defined]
    assert consumidor_falso.creados == []


def test_con_cola_y_base_el_consumidor_corre_y_se_detiene_al_cerrar(
    consumidor_falso: type[ConsumidorFalso],
) -> None:
    settings = Settings(sqs_consulta_proyeccion_url=COLA)
    with TestClient(crear_app(settings, pool=PoolFalso())) as cliente:  # type: ignore[arg-type]
        tarea = cliente.app.state.consumidor_proyeccion  # type: ignore[attr-defined]
        assert len(consumidor_falso.creados) == 1
        assert not tarea.done()
    assert consumidor_falso.creados[0].parado


def test_con_cola_pero_sin_base_la_app_arranca_sin_consumidor(
    consumidor_falso: type[ConsumidorFalso],
) -> None:
    settings = Settings(sqs_consulta_proyeccion_url=COLA)
    with TestClient(crear_app(settings)) as cliente:
        assert cliente.get("/health").json() == {"status": "ok"}
    assert consumidor_falso.creados == []


def test_si_no_se_puede_crear_el_cliente_sqs_la_app_arranca(
    consumidor_falso: type[ConsumidorFalso], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fallar(*_: object, **__: object) -> None:
        raise RuntimeError("sin credenciales")

    monkeypatch.setattr(main, "cliente_sqs", fallar)
    settings = Settings(sqs_consulta_proyeccion_url=COLA)
    with TestClient(crear_app(settings, pool=PoolFalso())) as cliente:  # type: ignore[arg-type]
        assert cliente.get("/health").json() == {"status": "ok"}
    assert consumidor_falso.creados == []


def test_health_responde_503_si_la_base_no_esta_disponible() -> None:
    with TestClient(crear_app(Settings(), pool=PoolCaido())) as cliente:  # type: ignore[arg-type]
        respuesta = cliente.get("/health")
    assert (respuesta.status_code, respuesta.json()) == (503, {"status": "base no disponible"})


def test_un_error_inesperado_del_consumidor_no_tumba_la_app(
    consumidor_falso: type[ConsumidorFalso], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fallar(self: ConsumidorFalso, parada: asyncio.Event) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(ConsumidorFalso, "ejecutar", fallar)
    settings = Settings(sqs_consulta_proyeccion_url=COLA)
    with TestClient(crear_app(settings, pool=PoolFalso())) as cliente:  # type: ignore[arg-type]
        assert cliente.get("/health").json() == {"status": "ok"}
