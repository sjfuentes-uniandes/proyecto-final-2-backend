"""Inbox y consumidor SQS (HU-W16, 16.5): sobre, modos del manejador, fallos, bucle y adaptadores
con puertos falsos.

Las pruebas con PostgreSQL y LocalStack reales están en `tests/module/test_inbox_consumidor.py`.
"""

import asyncio
import json
import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import anyio
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from solventa_common import correlation, inbox
from solventa_common.inbox import (
    ConsumidorInbox,
    Continuacion,
    EventoInvalido,
    EventoRecibido,
    InboxPg,
    MensajeSqs,
    ReceptorSqs,
    SobreInvalido,
    cliente_sqs,
    leer_sobre,
)

CENTINELA = "Juan Pérez 1032456789"
INSTANTE = datetime(2026, 10, 28, 13, 5, 26, 120, tzinfo=UTC)
COLA = "https://sqs.us-east-1.amazonaws.com/000000000000/dinero-aceptaciones"


def _payload() -> dict[str, Any]:
    aceptacion = str(uuid4())
    return {
        "aceptacionId": aceptacion,
        "prima": {"valor": "1850000.50", "moneda": "COP", "periodicidad": "ANUAL"},
        "claveRecaudo": aceptacion,
        "actorId": "asesor-sintetico-01",
    }


def _sobre(
    tipo: str = "OfertaAceptada",
    *,
    event_id: UUID | None = None,
    payload: dict[str, Any] | None = None,
    correlation_id: str = "corr-sintetico-01",
) -> dict[str, Any]:
    return {
        "eventId": str(event_id or uuid4()),
        "aggregateId": str(uuid4()),
        "type": tipo,
        "version": 1,
        "occurredAt": "2026-10-28T13:05:26Z",
        "correlationId": correlation_id,
        "payload": payload if payload is not None else _payload(),
    }


def _mensaje(
    sobre: dict[str, Any] | None = None,
    *,
    cuerpo: str | None = None,
    atributo: str | None = "OfertaAceptada",
    recepciones: int | None = 1,
) -> MensajeSqs:
    return MensajeSqs(
        message_id=f"msg-{uuid4().hex[:8]}",
        receipt_handle=f"rh-{uuid4().hex}",
        cuerpo=cuerpo if cuerpo is not None else json.dumps(sobre or _sobre(), ensure_ascii=False),
        tipo_atributo=atributo,
        recepciones=recepciones,
    )


def _evento_de(mensaje: MensajeSqs) -> UUID:
    return UUID(json.loads(mensaje.cuerpo)["eventId"])


# --- Puertos falsos -------------------------------------------------------------------------
@dataclass
class TransaccionFalsa:
    """Los cambios se aplican al repositorio solo si la transacción termina sin error."""

    repositorio: "RepositorioFalso"
    nuevos: dict[UUID, tuple[str, datetime]] = field(default_factory=dict)
    efectos: list[UUID] = field(default_factory=list)

    async def registrar(self, event_id: UUID, event_type: str, recibido_en: datetime) -> bool:
        self.repositorio.bitacora.append("registrar")
        if event_id in self.repositorio.inbox or event_id in self.nuevos:
            return False
        self.nuevos[event_id] = (event_type, recibido_en)
        return True


@dataclass
class RepositorioFalso:
    bitacora: list[str]
    inbox: dict[UUID, tuple[str, datetime]] = field(default_factory=dict)
    efectos: list[UUID] = field(default_factory=list)
    transacciones: int = 0
    falla_al_abrir: int = 0

    @asynccontextmanager
    async def transaccion(self) -> AsyncIterator[TransaccionFalsa]:
        self.transacciones += 1
        if self.falla_al_abrir:
            self.falla_al_abrir -= 1
            raise ConnectionError("base de datos no disponible")
        tx = TransaccionFalsa(self)
        try:
            yield tx
        except BaseException:
            self.bitacora.append("rollback")
            raise
        self.inbox.update(tx.nuevos)
        self.efectos.extend(tx.efectos)
        self.bitacora.append("commit")


@dataclass
class ReceptorFalso:
    bitacora: list[str]
    lotes: list[list[MensajeSqs] | BaseException] = field(default_factory=list)
    borrados: list[str] = field(default_factory=list)
    falla_al_borrar: BaseException | None = None
    recepciones: int = 0

    async def recibir(self) -> list[MensajeSqs]:
        self.recepciones += 1
        if not self.lotes:
            await asyncio.sleep(0.01)  # como la espera larga sin mensajes
            return []
        await asyncio.sleep(0)
        lote = self.lotes.pop(0)
        if isinstance(lote, BaseException):
            raise lote
        return lote

    async def borrar(self, mensaje: MensajeSqs) -> None:
        self.bitacora.append("borrar")
        if self.falla_al_borrar is not None:
            raise self.falla_al_borrar
        self.borrados.append(mensaje.message_id)


@dataclass
class ManejadorFalso:
    """Modo (a) sin `continuacion`; modo (b) con ella. Escribe un efecto en la transacción."""

    bitacora: list[str]
    continuacion: bool = False
    falla: BaseException | None = None
    falla_continuacion: BaseException | None = None
    pausa_s: float = 0
    eventos: list[EventoRecibido] = field(default_factory=list)
    transacciones: list[TransaccionFalsa] = field(default_factory=list)
    continuaciones: int = 0
    correlaciones: list[str | None] = field(default_factory=list)

    async def __call__(self, evento: EventoRecibido, tx: TransaccionFalsa) -> Continuacion | None:
        self.bitacora.append("manejador")
        self.eventos.append(evento)
        self.transacciones.append(tx)
        self.correlaciones.append(correlation.obtener())
        logging.getLogger("servicio.manejador").info("manejando el evento")
        if self.pausa_s:
            await asyncio.sleep(self.pausa_s)
            self.bitacora.append("fin manejador")
        if self.falla is not None:
            raise self.falla
        tx.efectos.append(evento.event_id)
        if not self.continuacion:
            return None

        async def continuar() -> None:
            self.bitacora.append("continuacion")
            self.continuaciones += 1
            self.correlaciones.append(correlation.obtener())
            if self.falla_continuacion is not None:
                raise self.falla_continuacion

        return continuar


@dataclass
class Escenario:
    bitacora: list[str]
    receptor: ReceptorFalso
    repositorio: RepositorioFalso
    manejador: ManejadorFalso

    def consumidor(self, **kwargs: Any) -> ConsumidorInbox[TransaccionFalsa]:
        return ConsumidorInbox(
            self.receptor, self.repositorio, self.manejador, reloj=lambda: INSTANTE, **kwargs
        )

    def entregar(self, *mensajes: MensajeSqs) -> None:
        self.receptor.lotes.append(list(mensajes))


def _escenario(**manejador: Any) -> Escenario:
    bitacora: list[str] = []
    return Escenario(
        bitacora,
        ReceptorFalso(bitacora),
        RepositorioFalso(bitacora),
        ManejadorFalso(bitacora, **manejador),
    )


def _registros(telemetria: Any) -> list[dict[str, Any]]:
    return [r for r in telemetria.logs() if r.get("operation") == inbox.OPERACION]


# --- Sobre --------------------------------------------------------------------------------------
def test_lee_el_sobre_sin_pasar_los_numeros_por_float() -> None:
    event_id = uuid4()
    sobre = _sobre(event_id=event_id, payload={"valor": 1850000.50, "cuotas": 3})
    mensaje = _mensaje(sobre, recepciones=2)

    evento = leer_sobre(mensaje)

    assert evento.event_id == event_id
    assert evento.event_type == "OfertaAceptada"
    assert evento.correlation_id == "corr-sintetico-01"
    assert evento.cuerpo == mensaje.cuerpo
    assert evento.message_id == mensaje.message_id
    assert evento.recepciones == 2
    assert evento.sobre["payload"]["valor"] == Decimal("1850000.50")
    assert isinstance(evento.sobre["payload"]["valor"], Decimal)
    assert evento.sobre["payload"]["cuotas"] == 3


@pytest.mark.parametrize("valor", ["", "con espacios", "x" * 129, 7])
def test_un_correlation_id_invalido_no_invalida_el_sobre(valor: Any) -> None:
    sobre = _sobre() | {"correlationId": valor}

    assert leer_sobre(_mensaje(sobre)).correlation_id is None


ENVENENADOS = [
    pytest.param("{no es json", inbox.MOTIVO_JSON, id="json-invalido"),
    pytest.param("", inbox.MOTIVO_JSON, id="cuerpo-vacio"),
    pytest.param('["eventId", "type"]', inbox.MOTIVO_JSON, id="no-es-objeto"),
    pytest.param(json.dumps({"type": "OfertaAceptada"}), inbox.MOTIVO_EVENT_ID, id="sin-event-id"),
    pytest.param(
        json.dumps(_sobre() | {"eventId": CENTINELA}), inbox.MOTIVO_EVENT_ID, id="event-id-no-uuid"
    ),
    pytest.param(
        json.dumps(_sobre() | {"eventId": 12}), inbox.MOTIVO_EVENT_ID, id="event-id-no-str"
    ),
    pytest.param(
        json.dumps({k: v for k, v in _sobre().items() if k != "type"}),
        inbox.MOTIVO_TYPE,
        id="sin-type",
    ),
    pytest.param(json.dumps(_sobre() | {"type": ""}), inbox.MOTIVO_TYPE, id="type-vacio"),
    pytest.param(json.dumps(_sobre() | {"type": None}), inbox.MOTIVO_TYPE, id="type-nulo"),
    pytest.param(
        json.dumps(_sobre() | {"type": CENTINELA}), inbox.MOTIVO_TYPE, id="type-no-nombre"
    ),
]


@pytest.mark.parametrize(("cuerpo", "motivo"), ENVENENADOS)
def test_un_sobre_envenenado_se_rechaza_con_su_motivo(cuerpo: str, motivo: str) -> None:
    with pytest.raises(SobreInvalido) as error:
        leer_sobre(_mensaje(cuerpo=cuerpo))

    assert error.value.motivo == motivo
    assert CENTINELA not in str(error.value)


# --- Modos del manejador ------------------------------------------------------------------------
@pytest.mark.anyio
async def test_modo_a_registra_y_aplica_el_efecto_en_una_transaccion_y_borra_tras_el_commit() -> (
    None
):
    escenario = _escenario()
    mensaje = _mensaje(recepciones=3)
    escenario.entregar(mensaje)

    assert await escenario.consumidor().ejecutar_una_vez() == 1

    event_id = _evento_de(mensaje)
    assert escenario.bitacora == ["registrar", "manejador", "commit", "borrar"]
    assert escenario.repositorio.inbox == {event_id: ("OfertaAceptada", INSTANTE)}
    assert escenario.repositorio.efectos == [event_id]
    assert escenario.receptor.borrados == [mensaje.message_id]
    (evento,) = escenario.manejador.eventos
    assert (evento.event_id, evento.event_type, evento.message_id, evento.recepciones) == (
        event_id,
        "OfertaAceptada",
        mensaje.message_id,
        3,
    )
    # El manejador recibe la transacción del inbox: su efecto y la fila van juntos.
    assert escenario.manejador.transacciones[0].repositorio is escenario.repositorio


@pytest.mark.anyio
async def test_modo_b_ejecuta_la_continuacion_tras_el_commit_y_antes_de_borrar() -> None:
    escenario = _escenario(continuacion=True)
    mensaje = _mensaje()
    escenario.entregar(mensaje)

    assert await escenario.consumidor().ejecutar_una_vez() == 1

    assert escenario.bitacora == ["registrar", "manejador", "commit", "continuacion", "borrar"]
    assert escenario.manejador.continuaciones == 1
    assert escenario.repositorio.efectos == [_evento_de(mensaje)]
    assert escenario.receptor.borrados == [mensaje.message_id]


@pytest.mark.anyio
@pytest.mark.parametrize("continuacion", [False, True], ids=["modo-a", "modo-b"])
async def test_un_duplicado_no_llama_al_manejador_hace_commit_y_borra(continuacion: bool) -> None:
    escenario = _escenario(continuacion=continuacion)
    sobre = _sobre()
    primero, segundo = _mensaje(sobre), _mensaje(sobre)
    escenario.entregar(primero)
    escenario.entregar(segundo)
    consumidor = escenario.consumidor()

    assert await consumidor.ejecutar_una_vez() == 1
    escenario.bitacora.clear()
    assert await consumidor.ejecutar_una_vez() == 1

    assert escenario.bitacora == ["registrar", "commit", "borrar"]
    assert len(escenario.manejador.eventos) == 1
    assert escenario.manejador.continuaciones == int(continuacion)
    assert len(escenario.repositorio.efectos) == 1
    assert escenario.receptor.borrados == [primero.message_id, segundo.message_id]


@pytest.mark.anyio
async def test_si_el_manejador_falla_hace_rollback_sin_fila_ni_borrado() -> None:
    escenario = _escenario(falla=ValueError("regla de negocio"))
    escenario.entregar(_mensaje())

    assert await escenario.consumidor().ejecutar_una_vez() == 0

    assert escenario.bitacora == ["registrar", "manejador", "rollback"]
    assert escenario.repositorio.inbox == {}
    assert escenario.repositorio.efectos == []
    assert escenario.receptor.borrados == []


@pytest.mark.anyio
async def test_tras_un_fallo_del_manejador_la_reentrega_lo_procesa() -> None:
    escenario = _escenario(falla=ValueError("transitorio"))
    mensaje = _mensaje()
    escenario.entregar(mensaje)
    consumidor = escenario.consumidor()
    await consumidor.ejecutar_una_vez()

    escenario.manejador.falla = None
    escenario.entregar(mensaje)
    assert await consumidor.ejecutar_una_vez() == 1

    assert escenario.repositorio.efectos == [_evento_de(mensaje)]
    assert escenario.receptor.borrados == [mensaje.message_id]


@pytest.mark.anyio
async def test_si_no_se_puede_abrir_la_transaccion_no_borra() -> None:
    escenario = _escenario()
    escenario.repositorio.falla_al_abrir = 1
    escenario.entregar(_mensaje())

    assert await escenario.consumidor().ejecutar_una_vez() == 0

    assert escenario.manejador.eventos == []
    assert escenario.receptor.borrados == []


@pytest.mark.anyio
async def test_si_la_continuacion_falla_no_borra_y_la_reentrega_es_un_duplicado() -> None:
    escenario = _escenario(continuacion=True, falla_continuacion=TimeoutError())
    mensaje = _mensaje()
    escenario.entregar(mensaje)
    consumidor = escenario.consumidor()

    assert await consumidor.ejecutar_una_vez() == 0
    assert escenario.bitacora == ["registrar", "manejador", "commit", "continuacion"]
    assert escenario.receptor.borrados == []
    # La parte transaccional ya quedó: la fila del inbox y el efecto.
    assert list(escenario.repositorio.inbox) == [_evento_de(mensaje)]

    escenario.bitacora.clear()
    escenario.entregar(mensaje)
    assert await consumidor.ejecutar_una_vez() == 1

    assert escenario.bitacora == ["registrar", "commit", "borrar"]
    assert len(escenario.manejador.eventos) == 1
    assert escenario.manejador.continuaciones == 1
    assert escenario.repositorio.efectos == [_evento_de(mensaje)]
    assert escenario.receptor.borrados == [mensaje.message_id]


@pytest.mark.anyio
async def test_procesa_el_lote_en_secuencia() -> None:
    escenario = _escenario(pausa_s=0.01)
    mensajes = [_mensaje() for _ in range(3)]
    escenario.entregar(*mensajes)

    assert await escenario.consumidor().ejecutar_una_vez() == 3

    assert escenario.bitacora == ["registrar", "manejador", "fin manejador", "commit", "borrar"] * 3
    assert [e.event_id for e in escenario.manejador.eventos] == [_evento_de(m) for m in mensajes]


@pytest.mark.anyio
async def test_devuelve_cuantos_proceso_entre_nuevos_duplicados_y_fallidos() -> None:
    escenario = _escenario()
    sobre = _sobre()
    escenario.entregar(
        _mensaje(sobre), _mensaje(sobre), _mensaje(cuerpo="{"), _mensaje(cuerpo="[]"), _mensaje()
    )

    assert await escenario.consumidor().ejecutar_una_vez() == 3

    assert len(escenario.receptor.borrados) == 3


@pytest.mark.anyio
async def test_sin_mensajes_no_abre_transacciones() -> None:
    escenario = _escenario()

    assert await escenario.consumidor().ejecutar_una_vez() == 0
    assert escenario.repositorio.transacciones == 0


@pytest.mark.anyio
async def test_si_el_borrado_falla_lo_registra_y_sigue(telemetria: Any) -> None:
    escenario = _escenario()
    escenario.receptor.falla_al_borrar = EndpointConnectionError(endpoint_url="http://sqs")
    primero, segundo = _mensaje(), _mensaje()
    escenario.entregar(primero, segundo)

    assert await escenario.consumidor().ejecutar_una_vez() == 2

    assert escenario.repositorio.efectos == [_evento_de(primero), _evento_de(segundo)]
    fallos = [r for r in telemetria.logs() if r.get("message") == inbox.MENSAJE_BORRADO_FALLIDO]
    assert [(r["level"], r["message_id"], r["error_type"]) for r in fallos] == [
        ("ERROR", primero.message_id, "EndpointConnectionError"),
        ("ERROR", segundo.message_id, "EndpointConnectionError"),
    ]


# --- Envenenados -----------------------------------------------------------------------------
@pytest.mark.anyio
@pytest.mark.parametrize(("cuerpo", "motivo"), ENVENENADOS)
async def test_un_mensaje_envenenado_no_llama_al_manejador_ni_se_borra(
    cuerpo: str, motivo: str, telemetria: Any
) -> None:
    escenario = _escenario()
    mensaje = _mensaje(cuerpo=cuerpo, recepciones=4)
    escenario.entregar(mensaje)

    assert await escenario.consumidor().ejecutar_una_vez() == 0

    assert escenario.manejador.eventos == []
    assert escenario.repositorio.transacciones == 0
    assert escenario.receptor.borrados == []
    (registro,) = _registros(telemetria)
    assert registro["level"] == "ERROR"
    assert registro["result"] == "error"
    assert registro["outcome"] == "envenenado"
    assert registro["reason"] == motivo
    assert registro["message_id"] == mensaje.message_id
    assert registro["receive_count"] == 4
    assert CENTINELA not in telemetria.texto_logs()
    if cuerpo:
        assert cuerpo not in telemetria.texto_logs()


@pytest.mark.anyio
async def test_un_evento_que_el_manejador_declara_invalido_es_envenenado(telemetria: Any) -> None:
    escenario = _escenario(falla=EventoInvalido())
    mensaje = _mensaje()
    escenario.entregar(mensaje)

    assert await escenario.consumidor().ejecutar_una_vez() == 0

    assert escenario.bitacora == ["registrar", "manejador", "rollback"]
    assert escenario.repositorio.inbox == {}
    assert escenario.receptor.borrados == []
    (registro,) = _registros(telemetria)
    assert (registro["outcome"], registro["reason"]) == ("envenenado", inbox.MOTIVO_ESQUEMA)
    assert registro["event_id"] == str(_evento_de(mensaje))


# --- Atributo eventType ---------------------------------------------------------------------------
@pytest.mark.anyio
@pytest.mark.parametrize(
    ("atributo", "registrado"),
    [("RecaudoConfirmado", "RecaudoConfirmado"), (None, None), (CENTINELA, None)],
    ids=["otro-tipo", "sin-atributo", "atributo-no-nombre"],
)
async def test_si_el_atributo_no_coincide_con_el_type_lo_registra_y_procesa(
    atributo: str | None, registrado: str | None, telemetria: Any
) -> None:
    escenario = _escenario()
    mensaje = _mensaje(_sobre("OfertaAceptada"), atributo=atributo)
    escenario.entregar(mensaje)

    assert await escenario.consumidor().ejecutar_una_vez() == 1

    avisos = [r for r in telemetria.logs() if r.get("message") == inbox.MENSAJE_TIPO_DISTINTO]
    assert [
        (r["level"], r["message_id"], r["event_type"], r["attribute_event_type"]) for r in avisos
    ] == [("WARNING", mensaje.message_id, "OfertaAceptada", registrado)]
    assert avisos[0]["correlation_id"] == "corr-sintetico-01"
    assert escenario.receptor.borrados == [mensaje.message_id]
    assert CENTINELA not in telemetria.texto_logs()


@pytest.mark.anyio
async def test_si_el_atributo_coincide_no_registra_aviso(telemetria: Any) -> None:
    escenario = _escenario()
    escenario.entregar(_mensaje(atributo="OfertaAceptada"))

    await escenario.consumidor().ejecutar_una_vez()

    assert not [r for r in telemetria.logs() if r.get("message") == inbox.MENSAJE_TIPO_DISTINTO]


# --- Telemetría y PII ----------------------------------------------------------------------------
@pytest.mark.anyio
async def test_mide_cada_mensaje_con_su_desenlace_y_su_correlacion(telemetria: Any) -> None:
    escenario = _escenario(continuacion=True)
    sobre = _sobre()
    nuevo, duplicado = _mensaje(sobre, recepciones=1), _mensaje(sobre, recepciones=2)
    escenario.entregar(nuevo, duplicado)

    await escenario.consumidor().ejecutar_una_vez()

    registros = {r["message_id"]: r for r in _registros(telemetria)}
    for mensaje, desenlace in ((nuevo, "nuevo"), (duplicado, "duplicado")):
        registro = registros[mensaje.message_id]
        assert registro["level"] == "INFO"
        assert registro["result"] == "ok"
        assert registro["outcome"] == desenlace
        assert registro["event_id"] == sobre["eventId"]
        assert registro["event_type"] == "OfertaAceptada"
        assert registro["correlation_id"] == "corr-sintetico-01"
        assert isinstance(registro["duration_ms"], int | float)
    assert registros[duplicado.message_id]["receive_count"] == 2
    # El manejador y la continuación corren con la correlación del sobre; después se restablece.
    assert escenario.manejador.correlaciones == ["corr-sintetico-01", "corr-sintetico-01"]
    (del_manejador,) = [r for r in telemetria.logs() if r["logger"] == "servicio.manejador"]
    assert del_manejador["correlation_id"] == "corr-sintetico-01"
    assert correlation.obtener() is None
    puntos = telemetria.puntos("OperationDuration")
    assert any(p.attributes == {"Operation": inbox.OPERACION} for p in puntos)


@pytest.mark.anyio
async def test_sin_correlation_id_valido_procesa_sin_correlacion(telemetria: Any) -> None:
    escenario = _escenario()
    escenario.entregar(_mensaje(_sobre(correlation_id="no válido")))

    assert await escenario.consumidor().ejecutar_una_vez() == 1

    assert escenario.manejador.correlaciones == [None]
    (registro,) = _registros(telemetria)
    assert (registro["outcome"], registro["correlation_id"]) == ("nuevo", None)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("manejador", "desenlace", "tipo_error"),
    [
        ({"falla": ValueError(CENTINELA)}, "fallo_transaccion", "ValueError"),
        (
            {"continuacion": True, "falla_continuacion": TimeoutError(CENTINELA)},
            "fallo_continuacion",
            "TimeoutError",
        ),
    ],
    ids=["manejador", "continuacion"],
)
async def test_un_fallo_se_mide_como_error_con_el_tipo_y_sin_el_mensaje(
    manejador: dict[str, Any], desenlace: str, tipo_error: str, telemetria: Any
) -> None:
    escenario = _escenario(**manejador)
    mensaje = _mensaje(_sobre(payload=_payload() | {"actorId": CENTINELA}))
    escenario.entregar(mensaje)

    await escenario.consumidor().ejecutar_una_vez()

    (registro,) = _registros(telemetria)
    assert (registro["level"], registro["result"]) == ("ERROR", "error")
    assert (registro["outcome"], registro["error_type"]) == (desenlace, tipo_error)
    assert registro["event_id"] == str(_evento_de(mensaje))
    texto = telemetria.texto_logs()
    assert CENTINELA not in texto
    assert "1850000.50" not in texto
    assert "claveRecaudo" not in texto


@pytest.mark.anyio
async def test_un_error_de_aws_en_el_manejador_registra_su_codigo(telemetria: Any) -> None:
    error = ClientError({"Error": {"Code": "ServiceUnavailable", "Message": CENTINELA}}, "X")
    escenario = _escenario(falla=error)
    escenario.entregar(_mensaje())

    await escenario.consumidor().ejecutar_una_vez()

    (registro,) = _registros(telemetria)
    assert registro["error_type"] == "ClientError: ServiceUnavailable"
    assert CENTINELA not in telemetria.texto_logs()


@pytest.mark.anyio
async def test_los_logs_no_llevan_el_cuerpo_ni_pii(telemetria: Any) -> None:
    escenario = _escenario(continuacion=True)
    payload = _payload() | {"actorId": CENTINELA}
    escenario.entregar(
        _mensaje(_sobre(payload=payload)),
        _mensaje(_sobre(payload=payload), atributo="RecaudoConfirmado"),
        _mensaje(cuerpo=json.dumps(_sobre(payload=payload) | {"eventId": "x"})),
    )

    await escenario.consumidor().ejecutar_una_vez()

    texto = telemetria.texto_logs()
    assert _registros(telemetria)
    assert CENTINELA not in texto
    assert "1850000.50" not in texto
    assert "aceptacionId" not in texto
    assert '"payload"' not in texto


# --- Bucle -------------------------------------------------------------------------------------------
class Esperas:
    """Sustituye la espera entre ciclos: anota los segundos y para tras `hasta` esperas."""

    def __init__(self, parada: asyncio.Event, hasta: int) -> None:
        self.parada = parada
        self.hasta = hasta
        self.segundos: list[float] = []

    async def __call__(self, parada: asyncio.Event, segundos: float) -> None:
        self.segundos.append(segundos)
        if len(self.segundos) >= self.hasta:
            self.parada.set()


@pytest.mark.anyio
async def test_un_fallo_de_recepcion_espera_cada_vez_mas_y_se_reinicia_al_recibir(
    monkeypatch: pytest.MonkeyPatch, telemetria: Any
) -> None:
    escenario = _escenario()
    falla = EndpointConnectionError(endpoint_url="http://sqs")
    escenario.receptor.lotes = [falla, falla, falla, falla, falla, [], falla]
    parada = asyncio.Event()
    esperas = Esperas(parada, hasta=6)
    monkeypatch.setattr(inbox, "_esperar", esperas)

    with anyio.fail_after(2):
        await escenario.consumidor(intervalo_s=1.0, intervalo_max_s=8.0).ejecutar(parada)

    assert esperas.segundos == [1.0, 2.0, 4.0, 8.0, 8.0, 1.0]
    fallos = [r for r in telemetria.logs() if r.get("message") == inbox.MENSAJE_RECEPCION_FALLIDA]
    assert len(fallos) == 6
    assert {(r["level"], r["error_type"]) for r in fallos} == {("ERROR", "EndpointConnectionError")}


@pytest.mark.anyio
async def test_un_mensaje_fallido_no_hace_esperar_al_bucle(monkeypatch: pytest.MonkeyPatch) -> None:
    escenario = _escenario(falla=ValueError("x"))
    escenario.entregar(_mensaje())
    escenario.receptor.lotes.append(EndpointConnectionError(endpoint_url="http://sqs"))
    parada = asyncio.Event()
    esperas = Esperas(parada, hasta=1)
    monkeypatch.setattr(inbox, "_esperar", esperas)

    with anyio.fail_after(2):
        await escenario.consumidor().ejecutar(parada)

    # Solo esperó por el fallo de recepción, y como primer fallo seguido.
    assert esperas.segundos == [inbox.INTERVALO_S]
    assert escenario.manejador.eventos


@pytest.mark.anyio
async def test_procesa_mensajes_en_bucle_hasta_la_parada() -> None:
    escenario = _escenario()
    mensajes = [_mensaje() for _ in range(3)]
    for mensaje in mensajes:
        escenario.entregar(mensaje)
    parada = asyncio.Event()

    async def detener_al_borrar_todos() -> None:
        while len(escenario.receptor.borrados) < 3:
            await asyncio.sleep(0.01)
        parada.set()

    with anyio.fail_after(2):
        await asyncio.gather(escenario.consumidor().ejecutar(parada), detener_al_borrar_todos())
    assert escenario.receptor.borrados == [m.message_id for m in mensajes]


@pytest.mark.anyio
async def test_con_la_parada_ya_recibida_no_recibe_nada() -> None:
    escenario = _escenario()
    parada = asyncio.Event()
    parada.set()

    await escenario.consumidor().ejecutar(parada)

    assert escenario.receptor.recepciones == 0


@pytest.mark.anyio
async def test_tras_la_espera_vuelve_a_recibir() -> None:
    escenario = _escenario()
    mensaje = _mensaje()
    escenario.receptor.lotes = [ConnectionError("sqs"), ConnectionError("sqs"), [mensaje]]
    parada = asyncio.Event()

    async def detener_al_borrar() -> None:
        while not escenario.receptor.borrados:
            await asyncio.sleep(0.01)
        parada.set()

    with anyio.fail_after(2):
        await asyncio.gather(
            escenario.consumidor(intervalo_s=0.01, intervalo_max_s=0.02).ejecutar(parada),
            detener_al_borrar(),
        )
    assert escenario.receptor.borrados == [mensaje.message_id]


@pytest.mark.anyio
async def test_la_parada_interrumpe_la_espera_tras_un_fallo() -> None:
    escenario = _escenario()
    escenario.receptor.lotes = [ConnectionError("sqs")] * 3
    parada = asyncio.Event()

    async def detener() -> None:
        await asyncio.sleep(0.05)
        parada.set()

    with anyio.fail_after(2):
        await asyncio.gather(
            escenario.consumidor(intervalo_s=30, intervalo_max_s=60).ejecutar(parada), detener()
        )
    assert escenario.receptor.recepciones == 1


# --- Adaptador SQS --------------------------------------------------------------------------------
class ClienteSqsFalso:
    def __init__(self, respuesta: dict[str, Any], read_timeout: float = 25) -> None:
        self.meta = SimpleNamespace(config=SimpleNamespace(read_timeout=read_timeout))
        self.respuesta = respuesta
        self.recepciones: list[dict[str, Any]] = []
        self.borrados: list[dict[str, Any]] = []
        self.hilos: list[int] = []

    def receive_message(self, **kwargs: Any) -> dict[str, Any]:
        self.recepciones.append(kwargs)
        self.hilos.append(threading.get_ident())
        return self.respuesta

    def delete_message(self, **kwargs: Any) -> dict[str, Any]:
        self.borrados.append(kwargs)
        self.hilos.append(threading.get_ident())
        return {}


@pytest.mark.anyio
async def test_recibe_con_espera_larga_y_atributos_en_otro_hilo() -> None:
    cliente = ClienteSqsFalso(
        {
            "Messages": [
                {
                    "MessageId": "m-1",
                    "ReceiptHandle": "rh-1",
                    "Body": '{"a": 1}',
                    "MessageAttributes": {
                        "eventType": {"DataType": "String", "StringValue": "OfertaAceptada"}
                    },
                    "Attributes": {"ApproximateReceiveCount": "3"},
                },
                {"MessageId": "m-2", "ReceiptHandle": "rh-2", "Body": "x"},
            ]
        }
    )
    receptor = ReceptorSqs(cliente, COLA, espera_larga_s=20, max_mensajes=7)

    mensajes = await receptor.recibir()

    assert cliente.recepciones == [
        {
            "QueueUrl": COLA,
            "MaxNumberOfMessages": 7,
            "WaitTimeSeconds": 20,
            "MessageAttributeNames": ["eventType"],
            "MessageSystemAttributeNames": ["ApproximateReceiveCount"],
        }
    ]
    assert mensajes == [
        MensajeSqs("m-1", "rh-1", '{"a": 1}', "OfertaAceptada", 3),
        MensajeSqs("m-2", "rh-2", "x", None, None),
    ]
    assert cliente.hilos != [threading.get_ident()]


@pytest.mark.anyio
async def test_una_recepcion_sin_mensajes_devuelve_una_lista_vacia() -> None:
    assert await ReceptorSqs(ClienteSqsFalso({}), COLA).recibir() == []


@pytest.mark.anyio
async def test_borra_por_receipt_handle_en_otro_hilo() -> None:
    cliente = ClienteSqsFalso({})

    await ReceptorSqs(cliente, COLA).borrar(MensajeSqs("m-1", "rh-1", "{}", None, 1))

    assert cliente.borrados == [{"QueueUrl": COLA, "ReceiptHandle": "rh-1"}]
    assert cliente.hilos != [threading.get_ident()]


def test_el_receptor_usa_la_espera_larga_y_el_lote_por_defecto() -> None:
    cliente = ClienteSqsFalso({})

    receptor = ReceptorSqs(cliente, COLA)

    assert (receptor.espera_larga_s, receptor.max_mensajes) == (20, 5)
    assert (inbox.ESPERA_LARGA_S, inbox.MAX_MENSAJES) == (20, 5)


@pytest.mark.parametrize("read_timeout", [20, 19.5, None])
def test_el_receptor_rechaza_un_cliente_cuyo_timeout_no_supera_la_espera_larga(
    read_timeout: float | None,
) -> None:
    with pytest.raises(ValueError, match="timeout de lectura"):
        ReceptorSqs(ClienteSqsFalso({}, read_timeout=read_timeout), COLA, espera_larga_s=20)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("argumentos", "texto"),
    [
        ({"espera_larga_s": 0}, "espera_larga_s"),
        ({"espera_larga_s": 21}, "espera_larga_s"),
        ({"max_mensajes": 0}, "max_mensajes"),
        ({"max_mensajes": 11}, "max_mensajes"),
    ],
)
def test_el_receptor_valida_los_limites_de_sqs(argumentos: dict[str, int], texto: str) -> None:
    with pytest.raises(ValueError, match=texto):
        ReceptorSqs(ClienteSqsFalso({}, read_timeout=60), COLA, **argumentos)


def test_cliente_sqs_por_defecto_lee_mas_que_la_espera_larga() -> None:
    cliente = cliente_sqs("us-east-1")

    assert cliente.meta.config.read_timeout == 25
    assert cliente.meta.config.read_timeout > inbox.ESPERA_LARGA_S
    assert cliente.meta.config.connect_timeout == inbox.TIMEOUT_CONEXION_S
    assert cliente.meta.config.retries == {
        "total_max_attempts": inbox.INTENTOS_SQS,
        "mode": "standard",
    }
    # El cliente de la fábrica pasa la validación del receptor.
    ReceptorSqs(cliente, COLA)


def test_cliente_sqs_recibe_la_espera_el_margen_y_los_reintentos_por_parametro() -> None:
    cliente = cliente_sqs(
        "us-east-1",
        endpoint_url="http://localhost:4566",
        espera_larga_s=2,
        margen_lectura_s=1.5,
        timeout_conexion_s=0.7,
        intentos=4,
    )

    assert cliente.meta.config.read_timeout == 3.5
    assert cliente.meta.config.connect_timeout == 0.7
    assert cliente.meta.config.retries == {"total_max_attempts": 4, "mode": "standard"}
    assert cliente.meta.endpoint_url == "http://localhost:4566"
    assert cliente.meta.region_name == "us-east-1"
    ReceptorSqs(cliente, COLA, espera_larga_s=2)


@pytest.mark.parametrize(
    "argumentos",
    [
        {"margen_lectura_s": 0},
        {"margen_lectura_s": -1},
        {"espera_larga_s": 0},
        {"espera_larga_s": 21},
    ],
)
def test_cliente_sqs_no_admite_un_timeout_que_no_supere_la_espera(
    argumentos: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        cliente_sqs("us-east-1", **argumentos)


# --- Adaptador PostgreSQL --------------------------------------------------------------------------
class CursorFalso:
    def __init__(self, fila: tuple[Any, ...] | None) -> None:
        self.fila = fila

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self.fila


class ConexionFalsa:
    def __init__(self, fila: tuple[Any, ...] | None) -> None:
        self.fila = fila
        self.sentencias: list[tuple[str, Any]] = []
        self.transacciones: list[str] = []

    async def execute(self, consulta: Any, parametros: Any = None) -> CursorFalso:
        self.sentencias.append((consulta.as_string(), parametros))
        return CursorFalso(self.fila)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        try:
            yield
        except BaseException:
            self.transacciones.append("rollback")
            raise
        self.transacciones.append("commit")


class PoolFalso:
    def __init__(self, conexion: ConexionFalsa) -> None:
        self.conexion = conexion

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[ConexionFalsa]:
        yield self.conexion


@pytest.mark.anyio
@pytest.mark.parametrize(("fila", "nuevo"), [((uuid4(),), True), (None, False)])
async def test_inbox_pg_inserta_sin_conflicto_y_dice_si_era_nuevo(
    fila: tuple[Any, ...] | None, nuevo: bool
) -> None:
    conexion = ConexionFalsa(fila)
    event_id = uuid4()

    async with InboxPg(PoolFalso(conexion)).transaccion() as tx:  # type: ignore[arg-type]
        assert await tx.registrar(event_id, "OfertaAceptada", INSTANTE) is nuevo
        assert tx.conexion is conexion

    ((consulta, parametros),) = conexion.sentencias
    assert " ".join(consulta.split()) == (
        'INSERT INTO "inbox" (event_id, event_type, recibido_en) VALUES (%s, %s, %s) '
        "ON CONFLICT (event_id) DO NOTHING RETURNING event_id"
    )
    assert parametros == (event_id, "OfertaAceptada", INSTANTE)
    assert conexion.transacciones == ["commit"]


@pytest.mark.anyio
async def test_inbox_pg_usa_la_tabla_indicada_y_hace_rollback_si_falla() -> None:
    conexion = ConexionFalsa(None)

    with pytest.raises(ValueError, match="caída"):
        async with InboxPg(PoolFalso(conexion), tabla="inbox_otro").transaccion() as tx:  # type: ignore[arg-type]
            await tx.registrar(uuid4(), "OfertaAceptada", INSTANTE)
            raise ValueError("caída")

    assert 'INSERT INTO "inbox_otro"' in conexion.sentencias[0][0]
    assert conexion.transacciones == ["rollback"]


@pytest.mark.anyio
async def test_el_reloj_por_defecto_registra_en_utc() -> None:
    escenario = _escenario()
    escenario.entregar(_mensaje())
    antes = datetime.now(UTC)

    await ConsumidorInbox(
        escenario.receptor, escenario.repositorio, escenario.manejador
    ).ejecutar_una_vez()

    ((_, recibido_en),) = escenario.repositorio.inbox.values()
    assert recibido_en.tzinfo is not None
    assert antes <= recibido_en <= datetime.now(UTC)
