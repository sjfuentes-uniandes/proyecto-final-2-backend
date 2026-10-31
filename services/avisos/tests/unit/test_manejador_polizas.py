import json
import logging
from typing import Any
from uuid import UUID, uuid4

import pytest
from avisos.adapters.inbound.sqs.polizas import manejador_polizas
from solventa_common.inbox import EventoInvalido, EventoRecibido, MensajeSqs, leer_sobre

pytestmark = pytest.mark.anyio


class RegistrarFalso:
    def __init__(self, creadas: int = 1) -> None:
        self.creadas = creadas
        self.llamadas: list[tuple[UUID, UUID, UUID]] = []

    async def poliza_emitida(
        self, event_id: UUID, poliza_id: UUID, cliente_id: UUID, tx: object
    ) -> int:
        self.llamadas.append((event_id, poliza_id, cliente_id))
        return self.creadas


class EnviarFalso:
    def __init__(self) -> None:
        self.eventos: list[UUID | None] = []

    async def ejecutar(self, event_id: UUID | None = None) -> int:
        self.eventos.append(event_id)
        return 1


def _recibido(sobre: dict[str, Any]) -> EventoRecibido:
    return leer_sobre(MensajeSqs("m-1", "r-1", json.dumps(sobre), sobre["type"], 1))


def _manejador(registrar: RegistrarFalso, enviar: EnviarFalso) -> Any:
    return manejador_polizas(registrar, enviar)  # type: ignore[arg-type]


async def test_poliza_emitida_v1_crea_las_entregas_y_devuelve_el_primer_intento(
    sobre_poliza: Any,
) -> None:
    registrar, enviar = RegistrarFalso(), EnviarFalso()
    sobre = sobre_poliza()

    continuacion = await _manejador(registrar, enviar)(_recibido(sobre), object())

    assert registrar.llamadas == [
        (
            UUID(sobre["eventId"]),
            UUID(sobre["payload"]["polizaId"]),
            UUID(sobre["payload"]["clienteId"]),
        )
    ]
    assert enviar.eventos == []
    await continuacion()
    assert enviar.eventos == [UUID(sobre["eventId"])]


async def test_sin_dispositivos_no_hay_continuacion(sobre_poliza: Any) -> None:
    registrar, enviar = RegistrarFalso(creadas=0), EnviarFalso()

    assert await _manejador(registrar, enviar)(_recibido(sobre_poliza()), object()) is None


@pytest.mark.parametrize("tipo", ["RecaudoConfirmado", "SiniestroReportado", "PolizaCancelada"])
async def test_un_tipo_no_manejado_se_ignora_sin_error_a_nivel_debug(
    tipo: str, sobre_poliza: Any, caplog: pytest.LogCaptureFixture
) -> None:
    registrar, enviar = RegistrarFalso(), EnviarFalso()
    caplog.set_level(logging.DEBUG, logger="avisos.consumidor")

    resultado = await _manejador(registrar, enviar)(
        _recibido(sobre_poliza(sobre={"type": tipo})), object()
    )

    assert resultado is None
    assert registrar.llamadas == []
    assert [r.levelno for r in caplog.records if r.name == "avisos.consumidor"] == [logging.DEBUG]


@pytest.mark.parametrize("version", [2, 0, "1"])
async def test_version_distinta_de_1_lanza_y_no_crea_entregas(
    version: Any, sobre_poliza: Any
) -> None:
    registrar = RegistrarFalso()

    with pytest.raises(EventoInvalido):
        await _manejador(registrar, EnviarFalso())(
            _recibido(sobre_poliza(sobre={"version": version})), object()
        )

    assert registrar.llamadas == []


@pytest.mark.parametrize(
    "cambios", [{"clienteId": "no-es-uuid"}, {"polizaId": "x"}, {"clienteId": None}]
)
async def test_un_cuerpo_invalido_lanza_sin_registrar_valores(
    cambios: dict[str, Any], sobre_poliza: Any, caplog: pytest.LogCaptureFixture
) -> None:
    registrar = RegistrarFalso()
    caplog.set_level(logging.WARNING)

    with pytest.raises(EventoInvalido):
        await _manejador(registrar, EnviarFalso())(_recibido(sobre_poliza(**cambios)), object())

    assert registrar.llamadas == []
    assert "no-es-uuid" not in caplog.text


async def test_el_aviso_no_depende_de_los_datos_de_la_poliza(sobre_poliza: Any) -> None:
    registrar, enviar = RegistrarFalso(), EnviarFalso()
    sobre = sobre_poliza(numero="sin-formato", prima="no-es-un-monto", clienteId=str(uuid4()))

    assert await _manejador(registrar, enviar)(_recibido(sobre), object()) is not None
