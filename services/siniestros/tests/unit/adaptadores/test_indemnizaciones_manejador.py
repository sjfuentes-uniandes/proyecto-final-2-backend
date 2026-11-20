"""Manejador de `IndemnizacionConfirmada` e `IndemnizacionRechazada` v1 (HU-W23; DS3-25): valida el
cuerpo y delega en RegistrarResultadoPago, en la transacción del inbox."""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from siniestros.adapters.inbound.sqs.indemnizaciones import manejador_indemnizaciones
from siniestros.domain.siniestro.pago import EstadoPago, ResultadoPago
from solventa_common.inbox import EventoInvalido, EventoRecibido

pytestmark = pytest.mark.anyio
SINIESTRO = uuid4()


def confirmada(**cambios: Any) -> dict[str, Any]:
    datos: dict[str, Any] = {
        "indemnizacionId": str(uuid4()),
        "siniestroId": str(SINIESTRO),
        "referencia": "IND-88-2026-0442-01",
        "monto": "2500000.00",
        "moneda": "COP",
        "transaccionId": "TRX-1",
        "intentos": 1,
        "duplicadosDescartados": 2,
        "confirmadaEn": "2026-11-10T15:00:03Z",
    }
    datos.update(cambios)
    return datos


def rechazada(**cambios: Any) -> dict[str, Any]:
    datos: dict[str, Any] = {
        "indemnizacionId": str(uuid4()),
        "siniestroId": str(SINIESTRO),
        "referencia": "IND-88-2026-0442-01",
        "motivo": "BENEFICIARIO_NO_HABILITADO",
        "intentos": 2,
        "duplicadosDescartados": 0,
        "rechazadaEn": "2026-11-10T15:00:04Z",
    }
    datos.update(cambios)
    return datos


def sobre(tipo: str, payload: dict[str, Any], **cambios: Any) -> dict[str, Any]:
    datos: dict[str, Any] = {
        "eventId": str(uuid4()),
        "aggregateId": str(uuid4()),
        "type": tipo,
        "version": 1,
        "occurredAt": "2026-11-10T15:00:03Z",
        "correlationId": "corr-w23",
        "payload": payload,
    }
    datos.update(cambios)
    return datos


def recibido(cuerpo: str, tipo: str = "IndemnizacionConfirmada") -> EventoRecibido:
    return EventoRecibido(
        event_id=uuid4(),
        event_type=tipo,
        correlation_id="corr-w23",
        sobre={},
        cuerpo=cuerpo,
        message_id="m-1",
        recepciones=1,
    )


class Caso:
    def __init__(self) -> None:
        self.llamadas: list[tuple[ResultadoPago, Any]] = []

    async def registrar(self, resultado: ResultadoPago, tx: Any) -> None:
        self.llamadas.append((resultado, tx))


def _manejador(caso: Caso) -> Any:
    return manejador_indemnizaciones(caso, lambda tx: ("tx-siniestros", tx))  # type: ignore[arg-type]


async def test_una_confirmada_valida_delega_con_la_transaccion_del_inbox() -> None:
    caso = Caso()
    cuerpo = json.dumps(sobre("IndemnizacionConfirmada", confirmada()))
    assert (
        await _manejador(caso)(recibido(cuerpo), "tx-inbox") is None
    )  # modo (a): sin continuación
    [(resultado, tx)] = caso.llamadas
    assert tx == ("tx-siniestros", "tx-inbox")
    assert resultado == ResultadoPago(
        siniestro_id=SINIESTRO,
        estado=EstadoPago.CONFIRMADO,
        referencia="IND-88-2026-0442-01",
        transaccion_id="TRX-1",
        motivo=None,
        intentos=1,
        duplicados_descartados=2,
        ocurrido_en=datetime(2026, 11, 10, 15, 0, 3, tzinfo=UTC),
    )


async def test_una_rechazada_valida_delega_con_su_motivo() -> None:
    caso = Caso()
    cuerpo = json.dumps(sobre("IndemnizacionRechazada", rechazada()))
    await _manejador(caso)(recibido(cuerpo, "IndemnizacionRechazada"), "tx")
    [(resultado, _)] = caso.llamadas
    assert (resultado.estado, resultado.motivo, resultado.transaccion_id) == (
        EstadoPago.RECHAZADO,
        "BENEFICIARIO_NO_HABILITADO",
        None,
    )
    assert resultado.ocurrido_en == datetime(2026, 11, 10, 15, 0, 4, tzinfo=UTC)


async def test_un_lector_tolerante_ignora_los_campos_que_no_usa() -> None:
    caso = Caso()
    cuerpo = json.dumps(sobre("IndemnizacionConfirmada", confirmada(campoNuevo="x"), otro=1))
    await _manejador(caso)(recibido(cuerpo), "tx")
    assert len(caso.llamadas) == 1


async def test_un_tipo_de_indemnizacion_que_siniestros_no_usa_se_consume_sin_efecto(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caso = Caso()
    caplog.set_level("INFO")
    cuerpo = json.dumps(sobre("IndemnizacionReversada", {}))
    assert await _manejador(caso)(recibido(cuerpo, "IndemnizacionReversada"), "tx") is None
    assert caso.llamadas == []
    assert "sin efecto" in caplog.text


@pytest.mark.parametrize(
    ("tipo", "cuerpo", "campo"),
    [
        (
            "IndemnizacionConfirmada",
            sobre("IndemnizacionConfirmada", confirmada(siniestroId="x")),
            "siniestroId",
        ),
        (
            "IndemnizacionConfirmada",
            sobre("IndemnizacionConfirmada", confirmada(referencia="IND-1")),
            "referencia",
        ),
        (
            "IndemnizacionConfirmada",
            sobre("IndemnizacionConfirmada", confirmada(transaccionId="")),
            "transaccionId",
        ),
        (
            "IndemnizacionConfirmada",
            sobre("IndemnizacionConfirmada", confirmada(intentos=0)),
            "intentos",
        ),
        (
            "IndemnizacionConfirmada",
            sobre("IndemnizacionConfirmada", confirmada(duplicadosDescartados=-1)),
            "duplicadosDescartados",
        ),
        (
            "IndemnizacionConfirmada",
            sobre("IndemnizacionConfirmada", confirmada(confirmadaEn="ayer")),
            "confirmadaEn",
        ),
        (
            "IndemnizacionRechazada",
            sobre("IndemnizacionRechazada", rechazada(motivo="texto libre")),
            "motivo",
        ),
        (
            "IndemnizacionConfirmada",
            sobre("IndemnizacionConfirmada", confirmada(), version=2),
            "version",
        ),
        (
            "IndemnizacionConfirmada",
            sobre("IndemnizacionConfirmada", confirmada(), eventId="no-uuid"),
            "eventId",
        ),
        ("IndemnizacionConfirmada", sobre("OtroTipo", confirmada()), ""),
    ],
)
async def test_un_cuerpo_que_no_valida_es_evento_invalido_y_nombra_el_campo(
    tipo: str, cuerpo: dict[str, Any], campo: str, caplog: pytest.LogCaptureFixture
) -> None:
    caso = Caso()
    with pytest.raises(EventoInvalido):
        await _manejador(caso)(recibido(json.dumps(cuerpo), tipo), "tx")
    assert caso.llamadas == []
    [aviso] = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any(campo in f for f in aviso.fields)  # type: ignore[attr-defined]
    assert aviso.message_id == "m-1"  # type: ignore[attr-defined]


async def test_el_aviso_no_lleva_valores_del_evento(caplog: pytest.LogCaptureFixture) -> None:
    cuerpo = sobre(
        "IndemnizacionConfirmada", confirmada(transaccionId="", referencia="SECRETO-XYZ")
    )
    with pytest.raises(EventoInvalido):
        await _manejador(Caso())(recibido(json.dumps(cuerpo)), "tx")
    assert "SECRETO-XYZ" not in caplog.text


@pytest.mark.parametrize("cuerpo", ["[]", "no es json", "{}"])
async def test_un_cuerpo_que_no_es_un_sobre_es_evento_invalido(cuerpo: str) -> None:
    with pytest.raises(EventoInvalido):
        await _manejador(Caso())(recibido(cuerpo), "tx")


def test_el_id_de_siniestro_es_un_uuid() -> None:
    assert isinstance(SINIESTRO, UUID)
