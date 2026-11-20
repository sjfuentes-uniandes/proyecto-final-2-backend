"""Manejador de `SiniestroAprobado` v1 (HU-W23; ADR 0015): valida el cuerpo y delega en
PagarIndemnizacion.

Lanza `EventoInvalido` (rollback, sin borrar: a la DLQ tras 5 recepciones) si el cuerpo no valida
contra el modelo de dinero, si la moneda no es COP o si `valorIndemnizar` tiene más de 2 decimales o
no es mayor que 0. Los logs nombran el campo, nunca su valor: ni el `beneficiarioRef`.
"""

import json
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from dinero.adapters.inbound.sqs.siniestro_aprobado import manejador_siniestro_aprobado
from dinero.application.indemnizacion.pagar import AprobacionInvalida, SiniestroAprobado
from solventa_common.inbox import EventoInvalido, EventoRecibido

pytestmark = pytest.mark.anyio


def payload(**cambios: Any) -> dict[str, Any]:
    datos: dict[str, Any] = {
        "siniestroId": str(uuid4()),
        "codigo": "88-2026-0442",
        "origen": "PORTAL",
        "polizaId": str(uuid4()),
        "clienteId": str(uuid4()),
        "valorIndemnizar": "2500000.00",
        "moneda": "COP",
        "referenciaPago": "IND-88-2026-0442-01",
        "beneficiarioRef": "ben-sint-001",
        "aprobadoEn": "2026-11-10T15:00:00Z",
        "decididoPor": str(uuid4()),
    }
    datos.update(cambios)
    return datos


def sobre(cuerpo_payload: Any = None, **cambios: Any) -> dict[str, Any]:
    datos: dict[str, Any] = {
        "eventId": str(uuid4()),
        "aggregateId": str(uuid4()),
        "type": "SiniestroAprobado",
        "version": 1,
        "occurredAt": "2026-11-10T15:00:00Z",
        "correlationId": "corr-w23-aprobado",
        "payload": payload() if cuerpo_payload is None else cuerpo_payload,
    }
    datos.update(cambios)
    return datos


def recibido(cuerpo: str, correlation_id: str | None = "corr-w23-aprobado") -> EventoRecibido:
    return EventoRecibido(
        event_id=uuid4(),
        event_type="SiniestroAprobado",
        correlation_id=correlation_id,
        sobre={},
        cuerpo=cuerpo,
        message_id="m-1",
        recepciones=1,
    )


class Caso:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.llamadas: list[tuple[SiniestroAprobado, Any]] = []

    async def registrar(self, aprobado: SiniestroAprobado, tx: Any) -> Any:
        self.llamadas.append((aprobado, tx))
        if self.error is not None:
            raise self.error
        return "continuacion"


async def test_un_evento_valido_delega_con_la_transaccion_y_devuelve_la_continuacion() -> None:
    caso = Caso()
    cuerpo_payload = payload(valorIndemnizar="2850000.5")
    evento = recibido(json.dumps(sobre(cuerpo_payload)))
    resultado = await manejador_siniestro_aprobado(caso)(evento, "tx-inbox")
    assert resultado == "continuacion"
    [(aprobado, tx)] = caso.llamadas
    assert tx == "tx-inbox"
    assert aprobado == SiniestroAprobado(
        siniestro_id=UUID(cuerpo_payload["siniestroId"]),
        referencia="IND-88-2026-0442-01",
        monto=Decimal("2850000.5"),
        moneda="COP",
        beneficiario_ref="ben-sint-001",
        correlation_id="corr-w23-aprobado",
    )
    assert isinstance(aprobado.monto, Decimal)


async def test_sin_correlation_id_del_sobre_se_genera_uno() -> None:
    caso = Caso()
    await manejador_siniestro_aprobado(caso)(
        recibido(json.dumps(sobre()), correlation_id=None), "tx"
    )
    [(aprobado, _)] = caso.llamadas
    assert aprobado.correlation_id and UUID(aprobado.correlation_id)


async def test_un_lector_tolerante_ignora_los_campos_que_no_usa() -> None:
    caso = Caso()
    extra = payload(campoNuevo="x")
    await manejador_siniestro_aprobado(caso)(recibido(json.dumps(sobre(extra, otro=1))), "tx")
    assert len(caso.llamadas) == 1


@pytest.mark.parametrize(
    ("cuerpo", "campos"),
    [
        (json.dumps(sobre(type="OtroEvento")), ["type"]),
        (json.dumps(sobre(version=2)), ["version"]),
        (json.dumps(sobre(eventId="no-uuid")), ["eventId"]),
        (json.dumps({"payload": {}}), ["__root__"]),
        (json.dumps(sobre(payload(siniestroId="no-uuid"))), ["payload.siniestroId"]),
        (json.dumps(sobre(payload(valorIndemnizar="abc"))), ["payload.valorIndemnizar"]),
        (json.dumps(sobre(payload(valorIndemnizar=2500000))), ["payload.valorIndemnizar"]),
        (json.dumps(sobre(payload(moneda="cop"))), ["payload.moneda"]),
        (json.dumps(sobre(payload(referenciaPago="IND-0442-01"))), ["payload.referenciaPago"]),
        (json.dumps(sobre(payload(beneficiarioRef=""))), ["payload.beneficiarioRef"]),
        (json.dumps(sobre(payload(beneficiarioRef="x" * 129))), ["payload.beneficiarioRef"]),
        ("[]", ["__root__"]),
    ],
)
async def test_un_cuerpo_que_no_valida_es_evento_invalido_con_los_campos(
    cuerpo: str, campos: list[str], logs: Any
) -> None:
    caso = Caso()
    with pytest.raises(EventoInvalido):
        await manejador_siniestro_aprobado(caso)(recibido(cuerpo), "tx")
    assert caso.llamadas == []
    [aviso] = logs.de_nivel("WARNING")
    assert aviso["message"] == "indemnizacion: SiniestroAprobado inválido; va a la DLQ"
    if campos != ["__root__"]:
        assert aviso["fields"] == campos
    assert aviso["message_id"] == "m-1"


async def test_una_aprobacion_que_incumple_el_dominio_es_evento_invalido(logs: Any) -> None:
    caso = Caso(AprobacionInvalida("monto: como máximo 2 decimales"))
    cuerpo = json.dumps(sobre(payload(beneficiarioRef="ben-sint-centinela")))
    with pytest.raises(EventoInvalido):
        await manejador_siniestro_aprobado(caso)(recibido(cuerpo), "tx")
    [aviso] = logs.de_nivel("WARNING")
    assert aviso["fields"] == ["monto"]
    assert "ben-sint-centinela" not in logs.texto()


async def test_el_aviso_no_lleva_el_valor_ni_el_beneficiario(logs: Any) -> None:
    cuerpo = json.dumps(sobre(payload(valorIndemnizar="secreto-xyz", beneficiarioRef="ben-sec")))
    with pytest.raises(EventoInvalido):
        await manejador_siniestro_aprobado(Caso())(recibido(cuerpo), "tx")
    assert "secreto-xyz" not in logs.texto() and "ben-sec" not in logs.texto()
