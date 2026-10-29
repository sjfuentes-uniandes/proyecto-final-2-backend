"""Manejador de `polizas-recaudos` y ProyectarRecaudo (HU-W16, 16.13), con los dobles.

Modo (a) de `solventa_common.inbox`: la proyección va en la transacción del inbox y no hay
continuación. Se queda el primer estado final por clave; un cuerpo inválido o un `type` ajeno es
`EventoInvalido` (a la DLQ).
"""

import io
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from polizas.adapters.inbound.sqs.recaudos import MENSAJE_INVALIDO, manejador_recaudos
from polizas.application.recaudo.proyectar import (
    MENSAJE_CLAVE_DESCONOCIDA,
    MENSAJE_CONFLICTO,
    ProyectarRecaudo,
)
from polizas.domain.recaudo.modelos import ResultadoRecaudo, SituacionRecaudo
from solventa_common.inbox import (
    ConsumidorInbox,
    EventoInvalido,
    EventoRecibido,
    MensajeSqs,
    leer_sobre,
)

pytestmark = pytest.mark.anyio
CONFIRMADO_EN = "2026-10-24T15:00:01.250000Z"
TX = object()
# Valores que nunca deben aparecer en un log.
TRANSACCION = "TX-SINTETICA-7731"
MONTO = "64638.10"


def _sobre(tipo: str, clave: UUID, **payload: Any) -> dict[str, Any]:
    recaudo_id = str(uuid4())
    base: dict[str, Any] = {
        "recaudoId": recaudo_id,
        "claveRecaudo": str(clave),
        "cotizacionId": str(uuid4()),
    }
    if tipo == "RecaudoConfirmado":
        base |= {
            "monto": {"valor": MONTO, "moneda": "COP"},
            "transaccionId": TRANSACCION,
            "confirmadoEn": CONFIRMADO_EN,
        }
    else:
        base |= {"motivo": "FONDOS_INSUFICIENTES", "rechazadoEn": CONFIRMADO_EN}
    return {
        "eventId": str(uuid4()),
        "aggregateId": recaudo_id,
        "type": tipo,
        "version": 1,
        "occurredAt": CONFIRMADO_EN,
        "correlationId": "corr-recaudo-1",
        "payload": base | payload,
    }


def _mensaje(sobre: dict[str, Any], message_id: str = "m-1") -> MensajeSqs:
    return MensajeSqs(message_id, f"r-{message_id}", json.dumps(sobre), sobre.get("type"), 1)


def _evento(sobre: dict[str, Any]) -> EventoRecibido:
    return leer_sobre(_mensaje(sobre))


def _manejar(dobles: Any) -> Any:
    return manejador_recaudos(ProyectarRecaudo(dobles.recaudos))


async def _aceptar(dobles: Any) -> UUID:
    """Una aceptación conocida por polizas (solo importa su id, que es la clave)."""

    class _Aceptacion:
        id = uuid4()

    dobles.aceptaciones.aceptaciones.append(_Aceptacion())
    return _Aceptacion.id


def _logs(salida: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(linea) for linea in salida.getvalue().splitlines() if linea.strip()]


def _sin_datos_del_cobro(salida: io.StringIO) -> None:
    texto = salida.getvalue()
    for centinela in (TRANSACCION, MONTO, "FONDOS_INSUFICIENTES"):
        assert centinela not in texto, centinela


async def test_confirmado_se_proyecta_en_la_transaccion_del_inbox(
    dobles: Any, capturar_logs: Callable[..., io.StringIO]
) -> None:
    salida = capturar_logs()
    clave = await _aceptar(dobles)
    sobre = _sobre("RecaudoConfirmado", clave)
    assert await _manejar(dobles)(_evento(sobre), TX) is None
    assert dobles.recaudos.resultados == {
        clave: ResultadoRecaudo(
            clave_recaudo=clave,
            recaudo_id=UUID(sobre["payload"]["recaudoId"]),
            estado=SituacionRecaudo.CONFIRMADO,
            monto=Decimal(MONTO),
            moneda="COP",
            transaccion_id=TRANSACCION,
            motivo=None,
            ocurrido_en=datetime(2026, 10, 24, 15, 0, 1, 250000, tzinfo=UTC),
        )
    }
    # El monto conserva su escala: no pasó por float.
    assert str(dobles.recaudos.resultados[clave].monto) == MONTO
    assert dobles.recaudos.transacciones == [TX]
    assert [log for log in _logs(salida) if log["level"] in ("WARNING", "ERROR")] == []


async def test_rechazado_se_proyecta_con_motivo_y_sin_monto(dobles: Any) -> None:
    clave = await _aceptar(dobles)
    sobre = _sobre("RecaudoRechazado", clave)
    assert await _manejar(dobles)(_evento(sobre), TX) is None
    resultado = dobles.recaudos.resultados[clave]
    assert (resultado.estado, resultado.motivo) == (
        SituacionRecaudo.RECHAZADO,
        "FONDOS_INSUFICIENTES",
    )
    assert (resultado.monto, resultado.moneda, resultado.transaccion_id) == (None, None, None)
    assert resultado.recaudo_id == UUID(sobre["payload"]["recaudoId"])
    assert resultado.ocurrido_en == datetime(2026, 10, 24, 15, 0, 1, 250000, tzinfo=UTC)


# --- Duplicados ------------------------------------------------------------------------------
class _TxMemoria:
    def __init__(self, vistos: set[UUID]) -> None:
        self._vistos = vistos

    async def registrar(self, event_id: UUID, event_type: str, recibido_en: datetime) -> bool:
        if event_id in self._vistos:
            return False
        self._vistos.add(event_id)
        return True


class _InboxMemoria:
    def __init__(self) -> None:
        self.vistos: set[UUID] = set()

    @asynccontextmanager
    async def transaccion(self) -> AsyncIterator[_TxMemoria]:
        yield _TxMemoria(self.vistos)


class _Receptor:
    def __init__(self, mensajes: list[MensajeSqs]) -> None:
        self._mensajes = mensajes
        self.borrados: list[str] = []

    async def recibir(self) -> list[MensajeSqs]:
        mensajes, self._mensajes = self._mensajes, []
        return mensajes

    async def borrar(self, mensaje: MensajeSqs) -> None:
        self.borrados.append(mensaje.message_id)


async def test_el_mismo_evento_dos_veces_tiene_un_solo_efecto(dobles: Any) -> None:
    clave = await _aceptar(dobles)
    sobre = _sobre("RecaudoConfirmado", clave)
    receptor = _Receptor([_mensaje(sobre, "m-1"), _mensaje(sobre, "m-2")])
    consumidor = ConsumidorInbox(receptor, _InboxMemoria(), _manejar(dobles))
    assert await consumidor.ejecutar_una_vez() == 2
    # El segundo es un duplicado del inbox: el manejador no corre y los dos se borran.
    assert len(dobles.recaudos.transacciones) == 1
    assert list(dobles.recaudos.resultados) == [clave]
    assert receptor.borrados == ["m-1", "m-2"]


async def test_el_mismo_estado_con_otro_event_id_no_cambia_nada(
    dobles: Any, capturar_logs: Callable[..., io.StringIO]
) -> None:
    salida = capturar_logs()
    clave = await _aceptar(dobles)
    primero = _sobre("RecaudoConfirmado", clave)
    await _manejar(dobles)(_evento(primero), TX)
    guardado = dobles.recaudos.resultados[clave]
    await _manejar(dobles)(_evento(_sobre("RecaudoConfirmado", clave)), TX)
    assert dobles.recaudos.resultados[clave] is guardado
    assert [log for log in _logs(salida) if log["level"] in ("WARNING", "ERROR")] == []


@pytest.mark.parametrize(
    ("primero", "despues"),
    [("RecaudoConfirmado", "RecaudoRechazado"), ("RecaudoRechazado", "RecaudoConfirmado")],
)
async def test_un_estado_final_distinto_no_cambia_el_primero_y_se_registra(
    dobles: Any, capturar_logs: Callable[..., io.StringIO], primero: str, despues: str
) -> None:
    salida = capturar_logs()
    clave = await _aceptar(dobles)
    await _manejar(dobles)(_evento(_sobre(primero, clave)), TX)
    guardado = dobles.recaudos.resultados[clave]
    # No lanza: el mensaje se borra (reintentar no lo arreglaría).
    assert await _manejar(dobles)(_evento(_sobre(despues, clave)), TX) is None
    assert dobles.recaudos.resultados[clave] is guardado
    [error] = [log for log in _logs(salida) if log["level"] == "ERROR"]
    assert error["message"] == MENSAJE_CONFLICTO
    assert (error["clave_recaudo"], error["estado"], error["estado_recibido"]) == (
        str(clave),
        guardado.estado.value,
        "CONFIRMADO" if despues == "RecaudoConfirmado" else "RECHAZADO",
    )
    assert error["logger"] == "polizas.recaudo"
    _sin_datos_del_cobro(salida)


async def test_una_clave_sin_aceptacion_se_guarda_y_se_avisa(
    dobles: Any, capturar_logs: Callable[..., io.StringIO]
) -> None:
    salida = capturar_logs()
    clave = uuid4()
    await _manejar(dobles)(_evento(_sobre("RecaudoConfirmado", clave)), TX)
    assert dobles.recaudos.resultados[clave].estado is SituacionRecaudo.CONFIRMADO
    [aviso] = [log for log in _logs(salida) if log["level"] == "WARNING"]
    assert aviso["message"] == MENSAJE_CLAVE_DESCONOCIDA
    assert (aviso["clave_recaudo"], aviso["estado"]) == (str(clave), "CONFIRMADO")
    _sin_datos_del_cobro(salida)


async def test_una_clave_conocida_no_avisa(
    dobles: Any, capturar_logs: Callable[..., io.StringIO]
) -> None:
    salida = capturar_logs()
    clave = await _aceptar(dobles)
    await _manejar(dobles)(_evento(_sobre("RecaudoRechazado", clave)), TX)
    assert [log for log in _logs(salida) if log["level"] == "WARNING"] == []


# --- Inválidos -------------------------------------------------------------------------------
def _sin(campo: str) -> Callable[[dict[str, Any]], None]:
    return lambda sobre: sobre["payload"].pop(campo)


def _con(campo: str, valor: Any) -> Callable[[dict[str, Any]], None]:
    return lambda sobre: sobre["payload"].__setitem__(campo, valor)


INVALIDOS_CONFIRMADO: dict[str, Callable[[dict[str, Any]], None]] = {
    "sin_transaccion": _sin("transaccionId"),
    "transaccion_vacia": _con("transaccionId", ""),
    "sin_clave": _sin("claveRecaudo"),
    "clave_no_uuid": _con("claveRecaudo", "no-es-uuid"),
    "monto_numero": _con("monto", {"valor": 64638.1, "moneda": "COP"}),
    "monto_tres_decimales": _con("monto", {"valor": "64638.100", "moneda": "COP"}),
    "monto_negativo": _con("monto", {"valor": "-1", "moneda": "COP"}),
    "moneda_invalida": _con("monto", {"valor": "1", "moneda": "pesos"}),
    "fecha_sin_zona": _con("confirmadoEn", "2026-10-24T15:00:01"),
    "version_2": lambda sobre: sobre.__setitem__("version", 2),
    "sin_payload": lambda sobre: sobre.pop("payload"),
}
INVALIDOS_RECHAZADO: dict[str, Callable[[dict[str, Any]], None]] = {
    "sin_motivo": _sin("motivo"),
    "motivo_minusculas": _con("motivo", "fondos insuficientes"),
    "sin_fecha": _sin("rechazadoEn"),
}


@pytest.mark.parametrize(
    ("tipo", "cambio"),
    [("RecaudoConfirmado", c) for c in INVALIDOS_CONFIRMADO.values()]
    + [("RecaudoRechazado", c) for c in INVALIDOS_RECHAZADO.values()],
    ids=list(INVALIDOS_CONFIRMADO) + list(INVALIDOS_RECHAZADO),
)
async def test_un_cuerpo_que_no_valida_es_evento_invalido(
    dobles: Any,
    capturar_logs: Callable[..., io.StringIO],
    tipo: str,
    cambio: Callable[[dict[str, Any]], None],
) -> None:
    salida = capturar_logs()
    sobre = _sobre(tipo, uuid4())
    cambio(sobre)
    with pytest.raises(EventoInvalido):
        await _manejar(dobles)(_evento(sobre), TX)
    assert dobles.recaudos.resultados == {}
    [aviso] = [log for log in _logs(salida) if log["level"] == "WARNING"]
    assert aviso["message"] == MENSAJE_INVALIDO
    assert (aviso["event_id"], aviso["message_id"], aviso["event_type"]) == (
        sobre["eventId"],
        "m-1",
        tipo,
    )
    assert aviso["fields"]
    _sin_datos_del_cobro(salida)


@pytest.mark.parametrize("tipo", ["RecaudoPendiente", "OfertaAceptada"])
async def test_un_type_que_no_es_ninguno_de_los_dos_es_evento_invalido(
    dobles: Any, capturar_logs: Callable[..., io.StringIO], tipo: str
) -> None:
    salida = capturar_logs()
    sobre = _sobre("RecaudoConfirmado", uuid4())
    sobre["type"] = tipo
    with pytest.raises(EventoInvalido):
        await _manejar(dobles)(_evento(sobre), TX)
    assert dobles.recaudos.resultados == {}
    [aviso] = [log for log in _logs(salida) if log["level"] == "WARNING"]
    assert (aviso["message"], aviso["event_type"], aviso["fields"]) == (
        MENSAJE_INVALIDO,
        tipo,
        ["type"],
    )


async def test_un_type_de_un_tipo_con_el_cuerpo_del_otro_es_invalido(dobles: Any) -> None:
    sobre = _sobre("RecaudoRechazado", uuid4())
    sobre["type"] = "RecaudoConfirmado"
    with pytest.raises(EventoInvalido):
        await _manejar(dobles)(_evento(sobre), TX)


async def test_el_consumidor_no_borra_un_invalido_y_lo_mide_como_envenenado(
    dobles: Any, telemetria: Any
) -> None:
    # Sin borrar: vuelve tras la visibilidad y, tras 5 recepciones, va a la DLQ.
    sobre = _sobre("RecaudoConfirmado", uuid4())
    sobre["payload"].pop("transaccionId")
    receptor = _Receptor([_mensaje(sobre)])
    consumidor = ConsumidorInbox(receptor, _InboxMemoria(), _manejar(dobles))
    assert await consumidor.ejecutar_una_vez() == 0
    assert receptor.borrados == []
    assert dobles.recaudos.resultados == {}
    [medicion] = [log for log in telemetria.logs() if log.get("operation") == "inbox.consumir"]
    assert (medicion["outcome"], medicion["reason"]) == ("envenenado", "esquema")
