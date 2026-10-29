"""Manejador de `OfertaAceptada` v1 (HU-W16, 16.10; P16-4): valida el cuerpo y delega en
RecaudarPrima.

Lanza `EventoInvalido` (rollback, sin borrar: a la DLQ tras 5 recepciones) si el cuerpo no valida
contra el modelo de dinero, si la moneda no es COP o si `prima.valor` tiene más de 2 decimales o no
es mayor que 0. Los logs nombran el campo, nunca su valor.
"""

import json
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from dinero.adapters.inbound.sqs.oferta_aceptada import manejador_oferta_aceptada
from dinero.application.recaudo.recaudar import OfertaAceptada, OfertaInvalida
from solventa_common.inbox import EventoInvalido, EventoRecibido

pytestmark = pytest.mark.anyio
CLIENTE = "00000000-0000-4000-8000-000000000001"


def payload(**cambios: Any) -> dict[str, Any]:
    aceptacion = str(uuid4())
    datos: dict[str, Any] = {
        "aceptacionId": aceptacion,
        "cotizacionId": str(uuid4()),
        "ofertaId": None,
        "decisionId": str(uuid4()),
        "clienteId": CLIENTE,
        "productoId": "VIDA-INDIVIDUAL",
        "prima": {"valor": "64638.07", "moneda": "COP", "periodicidad": "MENSUAL"},
        "claveRecaudo": aceptacion,
        "huellaCalculo": "sha256:abc",
        "vigenteHasta": "2026-11-27T13:05:26Z",
        "versionDeclaraciones": "2026.10.1",
        "aceptadaEn": "2026-10-28T13:05:26Z",
        "actorId": "asesor-sintetico-01",
        "canal": "PORTAL_ASESOR",
    }
    datos.update(cambios)
    return datos


def sobre(cuerpo_payload: Any = None, **cambios: Any) -> dict[str, Any]:
    datos: dict[str, Any] = {
        "eventId": str(uuid4()),
        "aggregateId": str(uuid4()),
        "type": "OfertaAceptada",
        "version": 1,
        "occurredAt": "2026-10-28T13:05:26Z",
        "correlationId": "corr-w16-oferta",
        "payload": payload() if cuerpo_payload is None else cuerpo_payload,
    }
    datos.update(cambios)
    return datos


def recibido(cuerpo: str, correlation_id: str | None = "corr-w16-oferta") -> EventoRecibido:
    return EventoRecibido(
        event_id=uuid4(),
        event_type="OfertaAceptada",
        correlation_id=correlation_id,
        sobre={},
        cuerpo=cuerpo,
        message_id="m-1",
        recepciones=1,
    )


class Caso:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.llamadas: list[tuple[OfertaAceptada, Any]] = []

    async def registrar(self, oferta: OfertaAceptada, tx: Any) -> Any:
        self.llamadas.append((oferta, tx))
        if self.error is not None:
            raise self.error
        return "continuacion"


async def manejar(cuerpo: str, caso: Caso | None = None, **kwargs: Any) -> tuple[Any, Caso]:
    caso = caso or Caso()
    resultado = await manejador_oferta_aceptada(caso)(recibido(cuerpo, **kwargs), "tx-inbox")  # type: ignore[arg-type]
    return resultado, caso


async def test_un_evento_valido_llega_al_caso_de_uso_con_la_transaccion_del_inbox() -> None:
    datos = sobre()
    resultado, caso = await manejar(json.dumps(datos))
    assert resultado == "continuacion"
    [(oferta, tx)] = caso.llamadas
    assert tx == "tx-inbox"
    entrada = datos["payload"]
    assert oferta == OfertaAceptada(
        clave_recaudo=UUID(entrada["claveRecaudo"]),
        cotizacion_id=UUID(entrada["cotizacionId"]),
        pagador_ref=UUID(CLIENTE),
        monto=Decimal("64638.07"),
        moneda="COP",
        periodicidad="MENSUAL",
        correlation_id="corr-w16-oferta",
    )


@pytest.mark.parametrize("valor", ["64638", "64638.00", "64638.1", "0.01", "999999999999.99"])
async def test_el_monto_llega_como_decimal_exacto(valor: str) -> None:
    prima = {"valor": valor, "moneda": "COP", "periodicidad": "ANUAL"}
    _, caso = await manejar(json.dumps(sobre(payload(prima=prima))))
    [(oferta, _)] = caso.llamadas
    assert type(oferta.monto) is Decimal
    assert str(oferta.monto) == valor


async def test_sin_correlacion_valida_genera_una_para_el_movimiento() -> None:
    """El inbox descarta un correlationId sin forma válida (no se propaga texto arbitrario)."""
    _, caso = await manejar(json.dumps(sobre(correlationId="con espacios")), correlation_id=None)
    [(oferta, _)] = caso.llamadas
    assert oferta.correlation_id != "con espacios"
    UUID(oferta.correlation_id)


@pytest.mark.parametrize(
    ("cuerpo", "campo"),
    [
        ("no es json", None),
        ("[]", None),
        (json.dumps(sobre(type="RecaudoConfirmado")), "type"),
        (json.dumps(sobre(version=2)), "version"),
        (json.dumps({k: v for k, v in sobre().items() if k != "payload"}), "payload"),
        (json.dumps(sobre(payload(claveRecaudo="no-es-uuid"))), "payload.claveRecaudo"),
        (
            json.dumps(sobre({k: v for k, v in payload().items() if k != "claveRecaudo"})),
            "payload.claveRecaudo",
        ),
        (
            json.dumps(sobre({k: v for k, v in payload().items() if k != "clienteId"})),
            "payload.clienteId",
        ),
        (
            json.dumps(sobre({k: v for k, v in payload().items() if k != "cotizacionId"})),
            "payload.cotizacionId",
        ),
        (
            json.dumps(
                sobre(payload(prima={"valor": 64638.07, "moneda": "COP", "periodicidad": "M"}))
            ),
            "payload.prima.valor",
        ),
        (
            json.dumps(sobre(payload(prima={"valor": "-1", "moneda": "COP", "periodicidad": "M"}))),
            "payload.prima.valor",
        ),
        (
            json.dumps(
                sobre(payload(prima={"valor": "1e3", "moneda": "COP", "periodicidad": "M"}))
            ),
            "payload.prima.valor",
        ),
        (
            json.dumps(sobre(payload(prima={"valor": "1", "moneda": "cop", "periodicidad": "M"}))),
            "payload.prima.moneda",
        ),
        (
            json.dumps(sobre(payload(prima={"valor": "1", "moneda": "COP"}))),
            "payload.prima.periodicidad",
        ),
    ],
    ids=[
        "no-json",
        "no-objeto",
        "otro-type",
        "otra-version",
        "sin-payload",
        "clave-no-uuid",
        "sin-clave",
        "sin-cliente",
        "sin-cotizacion",
        "valor-numero",
        "valor-negativo",
        "valor-cientifico",
        "moneda-minuscula",
        "sin-periodicidad",
    ],
)
async def test_un_cuerpo_que_no_valida_es_evento_invalido_sin_llegar_al_caso(
    cuerpo: str, campo: str | None, logs: Any
) -> None:
    with pytest.raises(EventoInvalido):
        await manejar(cuerpo)
    [aviso] = logs.de_nivel("WARNING")
    if campo is not None:
        assert campo in aviso["fields"]
    assert "64638.07" not in logs.texto() and CLIENTE not in logs.texto()


@pytest.mark.parametrize(
    ("prima", "campo"),
    [
        ({"valor": "64638.07", "moneda": "USD", "periodicidad": "MENSUAL"}, "moneda"),
        ({"valor": "64638.001", "moneda": "COP", "periodicidad": "MENSUAL"}, "monto"),
        ({"valor": "0", "moneda": "COP", "periodicidad": "MENSUAL"}, "monto"),
        ({"valor": "0.00", "moneda": "COP", "periodicidad": "MENSUAL"}, "monto"),
    ],
)
async def test_moneda_o_monto_fuera_de_regla_es_evento_invalido(
    prima: dict[str, str], campo: str, logs: Any
) -> None:
    """La regla vive en el dominio; aquí solo se traduce `OfertaInvalida` a `EventoInvalido`. El
    caso de uso real lanza `OfertaInvalida` (test_recaudar.py); el doble la simula."""
    caso = Caso(error=OfertaInvalida(f"{campo}: no admitido"))
    with pytest.raises(EventoInvalido):
        await manejar(json.dumps(sobre(payload(prima=prima))), caso)
    [aviso] = logs.de_nivel("WARNING")
    assert aviso["fields"] == [campo]
    # El valor como cadena JSON: buscar "0" suelto coincidiría con cualquier campo del log.
    assert f'"{prima["valor"]}"' not in logs.texto() and CLIENTE not in logs.texto()


async def test_otros_errores_del_caso_de_uso_no_son_evento_invalido() -> None:
    """Un fallo de la base no es un envenenado: el mensaje se reintenta (rollback)."""
    with pytest.raises(ConnectionError):
        await manejar(json.dumps(sobre()), Caso(error=ConnectionError("base caída")))
