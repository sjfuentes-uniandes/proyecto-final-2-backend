"""HU-W24 (N1): un objeto por lote recibido (caso de uso y consumidor) con puertos falsos.

Con PostgreSQL, S3 y SQS reales (LocalStack), en `tests/module/test_auditoria_lotes.py`.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import anyio
import pytest
from auditoria.adapters.inbound.sqs import lotes
from auditoria.adapters.inbound.sqs.lotes import ConsumidorLotes
from auditoria.adapters.outbound.almacen_s3 import AlmacenS3, clave_de
from auditoria.application.registrar_lote import RegistrarLote
from auditoria.domain.registro import GENESIS, EventoAuditable, Lote, verificar_cadena
from solventa_common.inbox import MensajeSqs

INSTANTE = datetime(2026, 11, 5, 14, 6, 12, tzinfo=UTC)
CENTINELA = "Juan Pérez 1032456789"


class Reloj:
    def ahora(self) -> datetime:
        return INSTANTE


@dataclass
class CadenaMemoria:
    """Inbox y cabeza en memoria; los cambios se aplican solo si la transacción termina bien."""

    inbox: set[str] = field(default_factory=set)
    secuencia: int = 0
    hash: str = GENESIS
    bitacora: list[str] = field(default_factory=list)

    @asynccontextmanager
    async def transaccion(self) -> AsyncIterator["TransaccionMemoria"]:
        tx = TransaccionMemoria(self)
        try:
            yield tx
        except BaseException:
            self.bitacora.append("rollback")
            raise
        self.inbox |= tx.nuevos
        if tx.cabeza_nueva is not None:
            self.secuencia, self.hash = tx.cabeza_nueva
        self.bitacora.append("commit")


@dataclass
class TransaccionMemoria:
    cadena: CadenaMemoria
    nuevos: set[str] = field(default_factory=set)
    cabeza_nueva: tuple[int, str] | None = None

    async def cabeza(self) -> tuple[int, str]:
        self.cadena.bitacora.append("cabeza")
        return self.cadena.secuencia, self.cadena.hash

    async def registrar(self, eventos: Any, recibido_en: datetime) -> set[str]:
        ids = {e.event_id for e in eventos} - self.cadena.inbox
        self.nuevos |= ids
        return ids

    async def avanzar(self, secuencia: int, hash_: str) -> None:
        self.cabeza_nueva = (secuencia, hash_)


@dataclass
class AlmacenMemoria:
    lotes: list[Lote] = field(default_factory=list)
    falla: BaseException | None = None
    bitacora: list[str] | None = None

    async def guardar(self, lote: Lote) -> None:
        if self.bitacora is not None:
            self.bitacora.append("guardar")
        if self.falla is not None:
            raise self.falla
        self.lotes.append(lote)


def _evento(event_id: str | None = None) -> EventoAuditable:
    return EventoAuditable(
        event_id=event_id or str(uuid4()),
        event_type="PolizaEmitida",
        aggregate_id=str(uuid4()),
        occurred_at="2026-11-05T14:06:12Z",
        correlation_id="corr-w24-01",
        actor="sistema",
        cuerpo="{}",
    )


def _caso() -> tuple[RegistrarLote, CadenaMemoria, AlmacenMemoria]:
    cadena = CadenaMemoria()
    almacen = AlmacenMemoria(bitacora=cadena.bitacora)
    return RegistrarLote(cadena, almacen, Reloj()), cadena, almacen


# --- Caso de uso ----------------------------------------------------------------------------------
@pytest.mark.anyio
async def test_un_lote_es_un_objeto_y_avanza_la_cabeza_en_la_transaccion() -> None:
    caso, cadena, almacen = _caso()

    resultado = await caso([_evento() for _ in range(10)])

    assert (resultado.nuevos, resultado.duplicados, resultado.secuencia) == (10, 0, 1)
    (lote,) = almacen.lotes
    assert len(lote.registros) == 10
    assert (cadena.secuencia, cadena.hash) == (1, lote.hash)
    # El bloqueo de la cabeza va antes que todo; el objeto, antes del commit.
    assert cadena.bitacora == ["cabeza", "guardar", "commit"]


@pytest.mark.anyio
async def test_lotes_seguidos_forman_una_cadena_que_verifica() -> None:
    caso, cadena, almacen = _caso()

    for _ in range(5):
        await caso([_evento() for _ in range(3)])

    documentos = [json.loads(lote.contenido()) for lote in almacen.lotes]
    assert [d["secuencia"] for d in documentos] == [1, 2, 3, 4, 5]
    assert verificar_cadena(documentos) == cadena.hash


@pytest.mark.anyio
async def test_los_duplicados_no_se_vuelven_a_escribir() -> None:
    caso, _, almacen = _caso()
    repetido = _evento()
    await caso([repetido])

    resultado = await caso([repetido, repetido, _evento()])

    assert (resultado.nuevos, resultado.duplicados, resultado.secuencia) == (1, 2, 2)
    assert [len(lote.registros) for lote in almacen.lotes] == [1, 1]


@pytest.mark.anyio
async def test_un_lote_solo_de_duplicados_no_escribe_ni_avanza() -> None:
    caso, cadena, almacen = _caso()
    evento = _evento()
    await caso([evento])

    resultado = await caso([evento])

    assert (resultado.nuevos, resultado.duplicados, resultado.secuencia) == (0, 1, None)
    assert len(almacen.lotes) == 1
    assert cadena.secuencia == 1


@pytest.mark.anyio
async def test_si_s3_falla_hace_rollback_sin_inbox_ni_cabeza() -> None:
    caso, cadena, almacen = _caso()
    almacen.falla = ConnectionError("s3")

    with pytest.raises(ConnectionError):
        await caso([_evento()])

    assert cadena.inbox == set()
    assert (cadena.secuencia, cadena.hash) == (0, GENESIS)
    assert cadena.bitacora[-1] == "rollback"


# --- Consumidor -----------------------------------------------------------------------------------
def _sobre(**campos: Any) -> str:
    return json.dumps(
        {
            "eventId": str(uuid4()),
            "aggregateId": str(uuid4()),
            "type": "PolizaEmitida",
            "version": 1,
            "occurredAt": "2026-11-05T14:06:12Z",
            "correlationId": "corr-w24-01",
            "payload": {"actorId": "asesor-sintetico-01", "nombre": CENTINELA},
        }
        | campos
    )


def _mensaje(cuerpo: str | None = None) -> MensajeSqs:
    return MensajeSqs(
        f"msg-{uuid4().hex[:8]}", f"rh-{uuid4().hex}", cuerpo or _sobre(), "PolizaEmitida", 1
    )


@dataclass
class Receptor:
    lotes: list[list[MensajeSqs]] = field(default_factory=list)
    pedidos: list[int] = field(default_factory=list)
    borrados: list[str] = field(default_factory=list)
    liberados: list[str] = field(default_factory=list)
    bitacora: list[str] | None = None

    async def recibir(self) -> list[MensajeSqs]:
        return await self.recibir_hasta(10)

    async def recibir_hasta(self, maximo: int) -> list[MensajeSqs]:
        self.pedidos.append(maximo)
        await asyncio.sleep(0.01)
        return self.lotes.pop(0) if self.lotes else []

    async def borrar(self, mensaje: MensajeSqs) -> None:
        if self.bitacora is not None:
            self.bitacora.append("borrar")
        self.borrados.append(mensaje.message_id)

    async def extender(self, mensaje: MensajeSqs, segundos: int) -> None:
        return None

    async def liberar(self, mensaje: MensajeSqs) -> None:
        self.liberados.append(mensaje.message_id)


def _registros(telemetria: Any) -> list[dict[str, Any]]:
    return [
        r
        for r in telemetria.logs()
        if r.get("operation") == lotes.OPERACION and r["logger"] == "solventa.telemetria"
    ]


@pytest.mark.anyio
async def test_recibe_lotes_de_diez_y_borra_despues_del_commit(telemetria: Any) -> None:
    caso, cadena, _ = _caso()
    receptor = Receptor(lotes=[[_mensaje() for _ in range(10)]], bitacora=cadena.bitacora)

    assert await ConsumidorLotes(receptor, caso).ejecutar_una_vez() == 10

    assert receptor.pedidos == [10]
    assert len(receptor.borrados) == 10
    assert cadena.bitacora == ["cabeza", "guardar", "commit"] + ["borrar"] * 10
    (registro,) = _registros(telemetria)
    assert (registro["messages"], registro["new"], registro["sequence"]) == (10, 10, 1)
    assert registro["result"] == "ok"


@pytest.mark.anyio
async def test_un_envenenado_no_se_borra_y_el_resto_del_lote_sigue(telemetria: Any) -> None:
    caso, _, _ = _caso()
    sanos = [_mensaje() for _ in range(3)]
    sin_sobre, incompleto = _mensaje("{no es json"), _mensaje(_sobre(occurredAt=None))
    receptor = Receptor(lotes=[[sanos[0], sin_sobre, *sanos[1:], incompleto]])

    assert await ConsumidorLotes(receptor, caso).ejecutar_una_vez() == 3

    assert sorted(receptor.borrados) == sorted(m.message_id for m in sanos)
    avisos = [r for r in telemetria.logs() if r.get("message") == lotes.MENSAJE_ENVENENADO]
    assert {(r["message_id"], r["reason"]) for r in avisos} == {
        (sin_sobre.message_id, "json_invalido"),
        (incompleto.message_id, lotes.MOTIVO_INCOMPLETO),
    }
    (registro,) = _registros(telemetria)
    assert (registro["messages"], registro["poisoned"], registro["new"]) == (5, 2, 3)


@pytest.mark.anyio
async def test_si_el_registro_falla_no_borra_nada(telemetria: Any) -> None:
    caso, _, almacen = _caso()
    almacen.falla = ConnectionError("s3")
    receptor = Receptor(lotes=[[_mensaje() for _ in range(4)]])

    assert await ConsumidorLotes(receptor, caso).ejecutar_una_vez() == 0

    assert receptor.borrados == []
    (registro,) = _registros(telemetria)
    assert (registro["result"], registro["error_type"]) == ("error", "ConnectionError")


@pytest.mark.anyio
async def test_los_logs_no_llevan_cuerpos_ni_pii(telemetria: Any) -> None:
    caso, _, _ = _caso()
    receptor = Receptor(lotes=[[_mensaje(), _mensaje("[" + CENTINELA)]])

    await ConsumidorLotes(receptor, caso).ejecutar_una_vez()

    assert CENTINELA not in telemetria.texto_logs()
    assert '"payload"' not in telemetria.texto_logs()


@pytest.mark.anyio
async def test_el_registro_guarda_el_actor_del_payload() -> None:
    caso, _, almacen = _caso()

    await ConsumidorLotes(Receptor(lotes=[[_mensaje()]]), caso).ejecutar_una_vez()

    (registro,) = almacen.lotes[0].registros
    assert registro["actor"] == "asesor-sintetico-01"


@pytest.mark.anyio
async def test_en_bucle_hasta_la_parada_y_libera_lo_que_llega_tarde() -> None:
    caso, _, almacen = _caso()
    receptor = Receptor(lotes=[[_mensaje()], [_mensaje()]])
    consumidor = ConsumidorLotes(receptor, caso)
    parada = asyncio.Event()

    async def detener() -> None:
        while len(receptor.borrados) < 2:
            await asyncio.sleep(0.005)
        tardio = _mensaje()

        async def lento(maximo: int) -> list[MensajeSqs]:
            await asyncio.sleep(0.05)
            return [tardio]

        receptor.recibir_hasta = lento  # type: ignore[method-assign]
        await asyncio.sleep(0.02)  # el consumidor ya está en la espera larga lenta
        parada.set()

    with anyio.fail_after(2):
        await asyncio.gather(consumidor.ejecutar(parada), detener())
    await asyncio.sleep(0.1)
    assert len(almacen.lotes) == 2
    assert len(receptor.liberados) == 1


# --- Almacén S3 -----------------------------------------------------------------------------------
class ClienteS3:
    def __init__(self) -> None:
        self.puestos: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.puestos.append(kwargs)
        return {}


@pytest.mark.anyio
@pytest.mark.parametrize("cifrado", [None, "aws:kms"])
async def test_escribe_sin_reemplazar_con_la_secuencia_y_el_hash_en_la_clave(
    cifrado: str | None,
) -> None:
    cliente = ClienteS3()
    almacen_memoria = AlmacenMemoria()
    await RegistrarLote(CadenaMemoria(), almacen_memoria, Reloj())([_evento()])
    (lote,) = almacen_memoria.lotes

    await AlmacenS3(cliente, "solventa-int-auditoria-1", cifrado=cifrado).guardar(lote)

    (puesto,) = cliente.puestos
    assert puesto["Key"] == f"cadena/000000000001-{lote.hash}.json" == clave_de(lote)
    assert puesto["IfNoneMatch"] == "*"
    assert json.loads(puesto["Body"]) == lote.documento()
    assert puesto.get("ServerSideEncryption") == cifrado
