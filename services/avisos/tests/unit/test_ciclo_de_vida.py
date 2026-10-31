"""Configuración y ciclo de vida de las tareas de fondo de `avisos` (HU-M11).

La infraestructura entrega `SQS_AVISOS_URL` a avisos (`consumes`, ecs.tf:82) y `AWS_REGION` a todos.
Con base, el barrido corre siempre; el consumidor solo si llega la cola. Si falta la base o el
cliente SQS no se puede crear, la aplicación sigue sirviendo.
"""

import asyncio
import json
from typing import Any, ClassVar, Self

import pytest
from avisos import main
from avisos.adapters.outbound.push_memoria import PushEnMemoria
from avisos.adapters.outbound.reloj import RelojSistema
from avisos.application.entregas.barrido import Barrido
from avisos.config import Settings
from avisos.main import crear_app
from fastapi.testclient import TestClient
from solventa_common import inbox

COLA = "https://sqs.us-east-1.amazonaws.com/000000000000/solventa-int-avisos"
_NOMBRES = ("SQS_AVISOS_URL", "AWS_REGION", "AWS_ENDPOINT_URL")


@pytest.fixture(autouse=True)
def sin_entorno(monkeypatch: pytest.MonkeyPatch) -> None:
    for nombre in _NOMBRES:
        monkeypatch.delenv(nombre, raising=False)
        monkeypatch.delenv(f"SOLVENTA_{nombre}", raising=False)


def test_valores_por_omision_de_las_entregas() -> None:
    settings = Settings()
    assert settings.sqs_avisos_url is None
    assert settings.consumidor_max_mensajes == inbox.MAX_MENSAJES
    assert (
        settings.entregas_max_intentos,
        settings.entregas_espera_inicial_s,
        settings.entregas_espera_max_s,
        settings.entregas_barrido_intervalo_s,
        settings.entregas_lease_s,
    ) == (5, 5.0, 300.0, 10.0, 60.0)


def test_la_cola_como_la_pone_ecs_y_su_variante_solventa(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SQS_AVISOS_URL", COLA)
    assert Settings().sqs_avisos_url == COLA
    monkeypatch.delenv("SQS_AVISOS_URL")
    monkeypatch.setenv("SOLVENTA_SQS_AVISOS_URL", COLA)
    assert Settings().sqs_avisos_url == COLA


@pytest.mark.parametrize(
    "campo",
    [
        {"consumidor_max_mensajes": 0},
        {"consumidor_max_mensajes": 11},
        {"consumidor_espera_larga_s": 11},
        {"entregas_max_intentos": 0},
        {"entregas_espera_inicial_s": 0},
        {"entregas_lease_s": 0},
    ],
)
def test_los_parametros_estan_acotados(campo: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Settings(**campo)


def test_la_parada_cabe_en_el_stop_timeout_de_ecs_y_el_lease_supera_el_timeout_del_envio() -> None:
    assert main.PARADA_TAREA_S > Settings().consumidor_espera_larga_s
    assert main.PARADA_TAREA_S <= 25
    assert Settings().entregas_lease_s > 2


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


class Tarea:
    creadas: ClassVar[list["Tarea"]] = []

    def __init__(self, *args: Any, **opciones: Any) -> None:
        self.parado = False
        Tarea.creadas.append(self)

    async def ejecutar(self, parada: asyncio.Event) -> None:
        await parada.wait()
        self.parado = True


@pytest.fixture
def tareas_falsas(monkeypatch: pytest.MonkeyPatch) -> type[Tarea]:
    Tarea.creadas = []
    monkeypatch.setattr(main, "ConsumidorInbox", Tarea)
    monkeypatch.setattr(main, "Barrido", Tarea)
    monkeypatch.setattr(main, "cliente_sqs", lambda *a, **k: object())
    monkeypatch.setattr(main, "ReceptorSqs", lambda *a, **k: object())
    return Tarea


def test_sin_cola_solo_corre_el_barrido(tareas_falsas: type[Tarea]) -> None:
    with TestClient(crear_app(Settings(), pool=PoolFalso())) as cliente:  # type: ignore[arg-type]
        assert cliente.get("/health").json() == {"status": "ok"}
        assert hasattr(cliente.app.state, "barrido")  # type: ignore[attr-defined]
        assert not hasattr(cliente.app.state, "consumidor_avisos")  # type: ignore[attr-defined]
    assert len(tareas_falsas.creadas) == 1
    assert tareas_falsas.creadas[0].parado


def test_con_cola_y_base_corren_el_barrido_y_el_consumidor_y_se_detienen_al_cerrar(
    tareas_falsas: type[Tarea],
) -> None:
    with TestClient(crear_app(Settings(sqs_avisos_url=COLA), pool=PoolFalso())) as cliente:  # type: ignore[arg-type]
        tareas = [
            cliente.app.state.barrido,  # type: ignore[attr-defined]
            cliente.app.state.consumidor_avisos,  # type: ignore[attr-defined]
        ]
        assert not any(t.done() for t in tareas)
    assert all(t.done() for t in tareas)
    assert all(t.parado for t in tareas_falsas.creadas)


def test_sin_base_no_se_consume_ni_se_barre_y_la_app_responde(
    tareas_falsas: type[Tarea],
) -> None:
    with TestClient(crear_app(Settings(sqs_avisos_url=COLA))) as cliente:
        assert cliente.get("/health").json() == {"status": "ok"}
    assert tareas_falsas.creadas == []


def test_si_el_cliente_sqs_no_se_crea_corre_solo_el_barrido(
    tareas_falsas: type[Tarea], monkeypatch: pytest.MonkeyPatch
) -> None:
    def roto(*args: Any, **opciones: Any) -> Any:
        raise ValueError("configuración inválida")

    monkeypatch.setattr(main, "cliente_sqs", roto)
    with TestClient(crear_app(Settings(sqs_avisos_url=COLA), pool=PoolFalso())) as cliente:  # type: ignore[arg-type]
        assert cliente.get("/health").status_code == 200
        assert not hasattr(cliente.app.state, "consumidor_avisos")  # type: ignore[attr-defined]


def _errores(salida: str) -> list[dict[str, Any]]:
    # La app instala su propio log JSON en stdout, por eso no se usa caplog.
    registros = [json.loads(linea) for linea in salida.splitlines() if linea.startswith("{")]
    return [r for r in registros if r["level"] == "ERROR"]


def test_sin_cola_se_registra_un_error_explicito_y_la_app_sigue(
    tareas_falsas: type[Tarea], capsys: pytest.CaptureFixture[str]
) -> None:
    with TestClient(crear_app(Settings(), pool=PoolFalso())) as cliente:  # type: ignore[arg-type]
        assert cliente.get("/health").status_code == 200
    errores = _errores(capsys.readouterr().out)
    assert [(r["message"], r["missing"]) for r in errores] == [
        (main.MENSAJE_SIN_COLA, "SQS_AVISOS_URL")
    ]


def test_si_el_cliente_sqs_no_se_crea_se_registra_un_error_sin_la_configuracion(
    tareas_falsas: type[Tarea], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def roto(*args: Any, **opciones: Any) -> Any:
        raise ValueError(f"credenciales inválidas para {COLA}")

    monkeypatch.setattr(main, "cliente_sqs", roto)
    with TestClient(crear_app(Settings(sqs_avisos_url=COLA), pool=PoolFalso())):  # type: ignore[arg-type]
        pass
    salida = capsys.readouterr().out
    assert [(r["message"], r["error_type"]) for r in _errores(salida)] == [
        (main.MENSAJE_CONSUMIDOR_NO_ARRANCO, "ValueError")
    ]
    assert COLA not in salida


def test_health_responde_503_si_la_base_cae(tareas_falsas: type[Tarea]) -> None:
    with TestClient(crear_app(Settings(), pool=PoolCaido())) as cliente:  # type: ignore[arg-type]
        respuesta = cliente.get("/health")
    assert (respuesta.status_code, respuesta.json()) == (503, {"status": "base no disponible"})


def test_una_tarea_que_falla_no_tumba_la_aplicacion(
    monkeypatch: pytest.MonkeyPatch, tareas_falsas: type[Tarea]
) -> None:
    async def falla(self: Tarea, parada: asyncio.Event) -> None:
        raise RuntimeError("inesperado")

    monkeypatch.setattr(Tarea, "ejecutar", falla)
    with TestClient(crear_app(Settings(), pool=PoolFalso())) as cliente:  # type: ignore[arg-type]
        assert cliente.get("/health").status_code == 200


def test_el_reloj_del_sistema_es_utc() -> None:
    assert RelojSistema().ahora().utcoffset().total_seconds() == 0  # type: ignore[union-attr]


@pytest.mark.anyio
async def test_el_barrido_repite_ciclos_sobrevive_a_un_fallo_y_se_detiene() -> None:
    class Casi:
        def __init__(self) -> None:
            self.ciclos = 0

        async def agotar_vencidas(self) -> int:
            self.ciclos += 1
            if self.ciclos == 1:
                raise RuntimeError("base caída")
            return 1 if self.ciclos == 2 else 0

        async def ejecutar(self, event_id: Any = None) -> int:
            return 0

    caso = Casi()
    parada = asyncio.Event()
    tarea = asyncio.create_task(Barrido(caso, 0.01).ejecutar(parada))  # type: ignore[arg-type]
    while caso.ciclos < 3:
        await asyncio.sleep(0.01)
    parada.set()
    await asyncio.wait_for(tarea, 1)

    assert caso.ciclos >= 3


def test_con_un_enviador_inyectado_no_se_avisa_del_envio_simulado(
    tareas_falsas: type[Tarea], capsys: pytest.CaptureFixture[str]
) -> None:
    with TestClient(
        crear_app(Settings(), pool=PoolFalso(), enviador=PushEnMemoria())  # type: ignore[arg-type]
    ):
        pass
    assert main.MENSAJE_PUSH_SIMULADO not in capsys.readouterr().out


def test_una_tarea_que_no_se_detiene_a_tiempo_se_cancela_y_la_app_cierra(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Terca(Tarea):
        async def ejecutar(self, parada: asyncio.Event) -> None:
            await asyncio.sleep(60)

    monkeypatch.setattr(main, "Barrido", Terca)
    monkeypatch.setattr(main, "PARADA_TAREA_S", 0.01)
    with TestClient(crear_app(Settings(), pool=PoolFalso())):  # type: ignore[arg-type]
        pass
    assert main.MENSAJE_TAREA_SIN_PARAR in capsys.readouterr().out
