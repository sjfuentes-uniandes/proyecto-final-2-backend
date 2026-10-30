import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from consulta.adapters.inbound.sqs.polizas import manejador_polizas
from consulta.application.proyeccion.comandos import PolizaEmitida
from consulta.application.proyeccion.proyectar_poliza import ProyectarPoliza
from solventa_common.inbox import EventoInvalido, EventoRecibido, MensajeSqs, leer_sobre

pytestmark = pytest.mark.anyio


class CasoFalso:
    def __init__(self) -> None:
        self.eventos: list[PolizaEmitida] = []

    async def ejecutar(self, evento: PolizaEmitida, tx: object) -> None:
        self.eventos.append(evento)


def _recibido(sobre: dict[str, Any]) -> EventoRecibido:
    return leer_sobre(MensajeSqs("m-1", "r-1", json.dumps(sobre), sobre["type"], 1))


def _manejador(caso: CasoFalso) -> Any:
    proyectar: ProyectarPoliza[object] = caso  # type: ignore[assignment]
    return manejador_polizas(proyectar)


async def test_poliza_emitida_v1_se_mapea_y_se_proyecta(sobre_poliza: Any) -> None:
    caso = CasoFalso()
    sobre = sobre_poliza()

    assert await _manejador(caso)(_recibido(sobre), object()) is None

    (evento,) = caso.eventos
    payload = sobre["payload"]
    assert str(evento.poliza_id) == payload["polizaId"]
    assert str(evento.cliente_id) == payload["clienteId"]
    assert (evento.producto_id, evento.producto_version) == ("vida-deudor", "1.0.0")
    assert (evento.prima, evento.suma_asegurada) == ("64638.10", "50000000")
    assert evento.documento_huella == payload["documento"]["huella"]
    assert evento.firma_id == "FIRMA-0001"
    assert evento.emitida_en == datetime(2026, 10, 24, 15, 0, 1, 250000, tzinfo=UTC)


@pytest.mark.parametrize("tipo", ["PolizaCancelada", "SiniestroReportado", "PolizaRenovada"])
async def test_un_tipo_no_manejado_se_ignora_sin_error_a_nivel_debug(
    tipo: str, sobre_poliza: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caso = CasoFalso()
    caplog.set_level(logging.DEBUG, logger="consulta.proyeccion")

    resultado = await _manejador(caso)(_recibido(sobre_poliza(sobre={"type": tipo})), object())

    assert resultado is None
    assert caso.eventos == []
    registros = [r for r in caplog.records if r.name == "consulta.proyeccion"]
    assert [r.levelno for r in registros] == [logging.DEBUG]


@pytest.mark.parametrize("version", [2, 0, "1"])
async def test_poliza_emitida_con_version_distinta_de_1_lanza_y_no_proyecta(
    version: Any, sobre_poliza: Any
) -> None:
    caso = CasoFalso()

    with pytest.raises(EventoInvalido):
        await _manejador(caso)(_recibido(sobre_poliza(sobre={"version": version})), object())

    assert caso.eventos == []


@pytest.mark.parametrize(
    "cambios",
    [
        {"numero": "sin-formato"},
        {"prima": "64638.123"},
        {"moneda": "cop"},
        {"clienteId": "no-es-uuid"},
        {"emitidaEn": "2026-10-24T15:00:01"},
    ],
)
async def test_un_cuerpo_invalido_lanza_sin_registrar_valores(
    cambios: dict[str, Any], sobre_poliza: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caso = CasoFalso()

    with pytest.raises(EventoInvalido):
        await _manejador(caso)(_recibido(sobre_poliza(**cambios)), object())

    assert caso.eventos == []
    texto = " ".join(str(vars(r)) for r in caplog.records)
    assert next(iter(cambios.values())) not in texto
