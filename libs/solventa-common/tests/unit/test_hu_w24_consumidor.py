"""HU-W24 (N1): contrapresión, latido de visibilidad, parada con liberación y causa del fallo del
consumidor común, con SQS simulado.

El comportamiento de HU-W16 (sin `concurrencia`) sigue cubierto en `test_inbox.py`; las pruebas con
LocalStack, en `tests/module/test_hu_w24_particiones.py`.
"""

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import anyio
import pytest
from solventa_common import correlation, inbox
from solventa_common.inbox import (
    ConsumidorInbox,
    Continuacion,
    Desenlace,
    EventoRecibido,
    MensajeSqs,
    ReceptorControlado,
    ReceptorSqs,
)

INSTANTE = datetime(2026, 11, 5, 14, 6, 12, tzinfo=UTC)
COLA = "https://sqs.us-east-1.amazonaws.com/000000000000/parametricos-eventos"


def _mensaje(*, correlacion: str = "corr-w24-01", cuerpo: str | None = None) -> MensajeSqs:
    sobre = {
        "eventId": str(uuid4()),
        "aggregateId": str(uuid4()),
        "type": "EventoParametricoObservado",
        "version": 1,
        "occurredAt": "2026-11-05T14:06:12Z",
        "correlationId": correlacion,
        "payload": {"precipitacionMm": "142.7"},
    }
    return MensajeSqs(
        message_id=f"msg-{uuid4().hex[:8]}",
        receipt_handle=f"rh-{uuid4().hex}",
        cuerpo=cuerpo if cuerpo is not None else json.dumps(sobre),
        tipo_atributo="EventoParametricoObservado",
        recepciones=1,
    )


# --- Puertos falsos -------------------------------------------------------------------------
@dataclass
class Transaccion:
    repositorio: "Repositorio"
    nuevos: set[UUID] = field(default_factory=set)

    async def registrar(self, event_id: UUID, event_type: str, recibido_en: datetime) -> bool:
        if event_id in self.repositorio.inbox or event_id in self.nuevos:
            return False
        self.nuevos.add(event_id)
        return True


@dataclass
class Repositorio:
    bitacora: list[tuple[str, str]]
    inbox: set[UUID] = field(default_factory=set)
    rollbacks: int = 0

    @asynccontextmanager
    async def transaccion(self) -> AsyncIterator[Transaccion]:
        tx = Transaccion(self)
        try:
            yield tx
        except BaseException:
            self.rollbacks += 1
            raise
        self.inbox |= tx.nuevos
        for event_id in tx.nuevos:
            self.bitacora.append(("commit", str(event_id)))


@dataclass
class ReceptorCola:
    """Una cola SQS simulada: entrega lo que hay, como mucho `maximo` y `lote` por recepción."""

    bitacora: list[tuple[str, str]]
    cola: list[MensajeSqs] = field(default_factory=list)
    lote: int = 10
    pedidos: list[int] = field(default_factory=list)
    borrados: list[str] = field(default_factory=list)
    liberados: list[str] = field(default_factory=list)
    extensiones: list[tuple[str, int]] = field(default_factory=list)
    falla_al_extender: BaseException | None = None
    espera_vacia_s: float = 0.01

    async def recibir(self) -> list[MensajeSqs]:
        return await self.recibir_hasta(self.lote)

    async def recibir_hasta(self, maximo: int) -> list[MensajeSqs]:
        self.pedidos.append(maximo)
        if not self.cola:
            await asyncio.sleep(self.espera_vacia_s)  # como la espera larga sin mensajes
            return []
        await asyncio.sleep(0)
        cuantos = min(maximo, self.lote)
        lote, self.cola = self.cola[:cuantos], self.cola[cuantos:]
        return lote

    async def borrar(self, mensaje: MensajeSqs) -> None:
        self.bitacora.append(("borrar", _evento(mensaje)))
        self.borrados.append(mensaje.message_id)

    async def extender(self, mensaje: MensajeSqs, segundos: int) -> None:
        if self.falla_al_extender is not None:
            raise self.falla_al_extender
        self.extensiones.append((mensaje.message_id, segundos))

    async def liberar(self, mensaje: MensajeSqs) -> None:
        self.liberados.append(mensaje.message_id)


def _evento(mensaje: MensajeSqs) -> str:
    try:
        return str(json.loads(mensaje.cuerpo)["eventId"])
    except (ValueError, KeyError):
        return mensaje.message_id


@dataclass
class Manejador:
    pausa_s: float = 0
    falla: BaseException | None = None
    activos: int = 0
    maximo_activos: int = 0
    correlaciones: list[tuple[str, str | None]] = field(default_factory=list)
    empezados: list[str] = field(default_factory=list)
    liberar: asyncio.Event | None = None

    async def __call__(self, evento: EventoRecibido, tx: Transaccion) -> Continuacion | None:
        self.activos += 1
        self.maximo_activos = max(self.maximo_activos, self.activos)
        self.empezados.append(evento.message_id)
        try:
            if self.liberar is not None:
                await self.liberar.wait()
            await asyncio.sleep(self.pausa_s)
            self.correlaciones.append((evento.message_id, correlation.obtener()))
            if self.falla is not None:
                raise self.falla
            return None
        finally:
            self.activos -= 1


@dataclass
class Escenario:
    bitacora: list[tuple[str, str]]
    receptor: ReceptorCola
    repositorio: Repositorio
    manejador: Manejador

    def consumidor(self, **kwargs: Any) -> ConsumidorInbox[Transaccion]:
        return ConsumidorInbox(
            self.receptor, self.repositorio, self.manejador, reloj=lambda: INSTANTE, **kwargs
        )

    def encolar(self, cuantos: int) -> list[MensajeSqs]:
        mensajes = [_mensaje(correlacion=f"corr-w24-{i:02d}") for i in range(cuantos)]
        self.receptor.cola.extend(mensajes)
        return mensajes


def _escenario(**manejador: Any) -> Escenario:
    bitacora: list[tuple[str, str]] = []
    return Escenario(
        bitacora, ReceptorCola(bitacora), Repositorio(bitacora), Manejador(**manejador)
    )


async def _hasta(condicion: Any, limite_s: float = 2) -> None:
    with anyio.fail_after(limite_s):
        while not condicion():
            await asyncio.sleep(0.005)


async def _correr_hasta(
    consumidor: ConsumidorInbox[Any], condicion: Any, limite_s: float = 2
) -> None:
    parada = asyncio.Event()

    async def detener() -> None:
        await _hasta(condicion, limite_s)
        parada.set()

    with anyio.fail_after(limite_s + 1):
        await asyncio.gather(consumidor.ejecutar(parada), detener())


def _registros(telemetria: Any) -> list[dict[str, Any]]:
    return [
        r
        for r in telemetria.logs()
        if r.get("operation") == inbox.OPERACION and r["logger"] == "solventa.telemetria"
    ]


# --- Configuración ---------------------------------------------------------------------------
def test_el_receptor_sqs_admite_el_control_de_visibilidad() -> None:
    cliente = SimpleNamespace(meta=SimpleNamespace(config=SimpleNamespace(read_timeout=25)))

    assert isinstance(ReceptorSqs(cliente, COLA), ReceptorControlado)


@dataclass
class ReceptorSinControl:
    async def recibir(self) -> list[MensajeSqs]:
        return []

    async def borrar(self, mensaje: MensajeSqs) -> None:
        return None


@pytest.mark.parametrize("argumentos", [{"concurrencia": 4}, {"visibilidad_s": 30}])
def test_la_contrapresion_y_el_latido_piden_un_receptor_controlado(
    argumentos: dict[str, int],
) -> None:
    escenario = _escenario()

    with pytest.raises(TypeError, match="ReceptorControlado"):
        ConsumidorInbox(
            ReceptorSinControl(), escenario.repositorio, escenario.manejador, **argumentos
        )


@pytest.mark.parametrize(
    ("argumentos", "texto"),
    [
        ({"concurrencia": 0}, "concurrencia"),
        ({"concurrencia": inbox.CONCURRENCIA_MAX + 1}, "concurrencia"),
        ({"visibilidad_s": 1}, "visibilidad_s"),
        ({"visibilidad_s": 4, "latido_s": 4}, "latido_s"),
    ],
)
def test_valida_los_limites(argumentos: dict[str, int], texto: str) -> None:
    with pytest.raises(ValueError, match=texto):
        _escenario().consumidor(**argumentos)


# --- Contrapresión (AC1) ---------------------------------------------------------------------
@pytest.mark.anyio
async def test_nunca_tiene_mas_mensajes_en_mano_que_su_concurrencia() -> None:
    escenario = _escenario(pausa_s=0.01)
    mensajes = escenario.encolar(25)
    consumidor = escenario.consumidor(concurrencia=4)
    en_mano: list[int] = []
    recibir = escenario.receptor.recibir_hasta

    async def anotar(maximo: int) -> list[MensajeSqs]:
        en_mano.append(consumidor.en_mano)
        return await recibir(maximo)

    escenario.receptor.recibir_hasta = anotar  # type: ignore[method-assign]

    await _correr_hasta(consumidor, lambda: len(escenario.receptor.borrados) == 25)

    assert sorted(escenario.receptor.borrados) == sorted(m.message_id for m in mensajes)
    # Pide solo los huecos libres: lo recibido más lo que tenía nunca supera la concurrencia.
    assert all(1 <= maximo <= 4 for maximo in escenario.receptor.pedidos)
    assert all(previo + pedido <= 4 for previo, pedido in zip(en_mano, escenario.receptor.pedidos))
    assert escenario.manejador.maximo_activos == 4


@pytest.mark.anyio
async def test_sin_huecos_no_recibe_y_lo_pendiente_queda_en_la_cola() -> None:
    liberar = asyncio.Event()
    escenario = _escenario(liberar=liberar)
    escenario.encolar(10)
    consumidor = escenario.consumidor(concurrencia=3)
    parada = asyncio.Event()

    tarea = asyncio.create_task(consumidor.ejecutar(parada))
    await _hasta(lambda: escenario.manejador.activos == 3)
    await asyncio.sleep(0.05)

    assert escenario.receptor.pedidos == [3]
    assert len(escenario.receptor.cola) == 7
    liberar.set()
    await _hasta(lambda: len(escenario.receptor.borrados) == 10)
    parada.set()
    await tarea


@pytest.mark.anyio
async def test_pide_como_mucho_el_lote_de_sqs() -> None:
    escenario = _escenario()
    escenario.encolar(30)
    consumidor = escenario.consumidor(concurrencia=25)

    await _correr_hasta(consumidor, lambda: len(escenario.receptor.borrados) == 30)

    assert escenario.receptor.pedidos[0] == inbox.LOTE_SQS
    assert max(escenario.receptor.pedidos) == inbox.LOTE_SQS


@pytest.mark.anyio
async def test_borra_cada_mensaje_solo_despues_de_su_commit() -> None:
    escenario = _escenario(pausa_s=0.005)
    escenario.encolar(12)

    await _correr_hasta(
        escenario.consumidor(concurrencia=5), lambda: len(escenario.receptor.borrados) == 12
    )

    for evento in {e for _, e in escenario.bitacora}:
        pasos = [paso for paso, e in escenario.bitacora if e == evento]
        assert pasos == ["commit", "borrar"]


@pytest.mark.anyio
async def test_un_fallo_no_se_borra_y_no_frena_a_los_demas() -> None:
    escenario = _escenario()
    mensajes = escenario.encolar(4)
    envenenado = _mensaje(cuerpo="{no es json")
    escenario.receptor.cola.insert(1, envenenado)

    await _correr_hasta(
        escenario.consumidor(concurrencia=2), lambda: len(escenario.receptor.borrados) == 4
    )

    assert sorted(escenario.receptor.borrados) == sorted(m.message_id for m in mensajes)
    assert envenenado.message_id not in escenario.receptor.liberados


@pytest.mark.anyio
async def test_cada_mensaje_corre_con_su_correlacion_aunque_vayan_a_la_vez() -> None:
    escenario = _escenario(pausa_s=0.01)
    mensajes = escenario.encolar(6)

    await _correr_hasta(
        escenario.consumidor(concurrencia=6), lambda: len(escenario.receptor.borrados) == 6
    )

    esperado = {m.message_id: json.loads(m.cuerpo)["correlationId"] for m in mensajes}
    assert dict(escenario.manejador.correlaciones) == esperado
    assert correlation.obtener() is None


@pytest.mark.anyio
async def test_mide_cada_mensaje_con_un_span_y_su_desenlace(telemetria: Any) -> None:
    escenario = _escenario()
    escenario.encolar(3)

    assert await escenario.consumidor(concurrencia=3).ejecutar_una_vez() == 3

    registros = _registros(telemetria)
    assert len(registros) == 3
    assert {r["outcome"] for r in registros} == {"nuevo"}
    assert {r["correlation_id"] for r in registros} == {"corr-w24-00", "corr-w24-01", "corr-w24-02"}
    spans = [s for s in telemetria.lista_spans() if s.name == inbox.OPERACION]
    assert len(spans) == 3
    assert {s.attributes["result"] for s in spans} == {"ok"}


# --- Visibilidad ------------------------------------------------------------------------------
@pytest.mark.anyio
async def test_el_latido_extiende_la_visibilidad_mientras_procesa_y_para_al_terminar() -> None:
    escenario = _escenario(pausa_s=0.12)
    (mensaje,) = escenario.encolar(1)
    consumidor = escenario.consumidor(concurrencia=1, visibilidad_s=30, latido_s=0.03)

    assert await consumidor.ejecutar_una_vez() == 1
    extensiones = list(escenario.receptor.extensiones)
    await asyncio.sleep(0.08)

    assert len(extensiones) >= 2
    assert set(extensiones) == {(mensaje.message_id, 30)}
    assert escenario.receptor.extensiones == extensiones


@pytest.mark.anyio
async def test_el_latido_cubre_tambien_a_los_que_esperan_turno() -> None:
    escenario = _escenario(pausa_s=0.08)
    primero, segundo = escenario.encolar(2)
    consumidor = escenario.consumidor(visibilidad_s=30, latido_s=0.03)

    assert await consumidor.ejecutar_una_vez() == 2

    extendidos = {message_id for message_id, _ in escenario.receptor.extensiones}
    assert extendidos == {primero.message_id, segundo.message_id}


@pytest.mark.anyio
async def test_si_extender_falla_lo_registra_y_el_mensaje_termina(telemetria: Any) -> None:
    escenario = _escenario(pausa_s=0.05)
    escenario.receptor.falla_al_extender = ConnectionError("sqs")
    (mensaje,) = escenario.encolar(1)

    assert await escenario.consumidor(visibilidad_s=30, latido_s=0.02).ejecutar_una_vez() == 1

    avisos = [r for r in telemetria.logs() if r.get("message") == inbox.MENSAJE_EXTENSION_FALLIDA]
    assert avisos
    assert {(r["level"], r["message_id"]) for r in avisos} == {("WARNING", mensaje.message_id)}
    assert escenario.receptor.borrados == [mensaje.message_id]


# --- Parada (SIGTERM) -------------------------------------------------------------------------
@pytest.mark.anyio
async def test_con_la_parada_libera_lo_que_espera_turno_y_termina_lo_que_esta_en_curso() -> None:
    liberar = asyncio.Event()
    escenario = _escenario(liberar=liberar)
    escenario.encolar(6)
    consumidor = escenario.consumidor(concurrencia=6, plazo_parada_s=2)
    # Dos a la vez dentro de un consumidor de seis: el resto espera turno.
    consumidor._semaforo = asyncio.Semaphore(2)
    parada = asyncio.Event()

    tarea = asyncio.create_task(consumidor.ejecutar(parada))
    await _hasta(lambda: escenario.manejador.activos == 2)
    parada.set()
    await asyncio.sleep(0.02)
    liberar.set()
    with anyio.fail_after(2):
        await tarea

    assert len(escenario.receptor.borrados) == 2
    assert len(escenario.receptor.liberados) == 4
    assert not set(escenario.receptor.borrados) & set(escenario.receptor.liberados)
    assert consumidor.en_mano == 0


@pytest.mark.anyio
async def test_si_el_plazo_vence_cancela_hace_rollback_y_libera(telemetria: Any) -> None:
    escenario = _escenario(liberar=asyncio.Event())  # nunca termina por sí solo
    (mensaje,) = escenario.encolar(1)
    consumidor = escenario.consumidor(concurrencia=2, plazo_parada_s=0.05)
    parada = asyncio.Event()

    tarea = asyncio.create_task(consumidor.ejecutar(parada))
    await _hasta(lambda: escenario.manejador.activos == 1)
    parada.set()
    with anyio.fail_after(2):
        await tarea

    assert escenario.receptor.borrados == []
    assert escenario.receptor.liberados == [mensaje.message_id]
    assert escenario.repositorio.rollbacks == 1
    assert escenario.repositorio.inbox == set()
    liberados = [r for r in telemetria.logs() if r.get("message") == inbox.MENSAJE_LIBERADO]
    assert [r["message_id"] for r in liberados] == [mensaje.message_id]


@pytest.mark.anyio
async def test_un_mensaje_que_llega_tras_la_parada_se_libera_sin_procesarse() -> None:
    escenario = _escenario()
    parada = asyncio.Event()
    tardio = _mensaje()
    consumidor = escenario.consumidor(concurrencia=2)

    async def entregar_tarde(maximo: int) -> list[MensajeSqs]:
        escenario.receptor.pedidos.append(maximo)
        await asyncio.sleep(0.05)
        return [tardio]

    escenario.receptor.recibir_hasta = entregar_tarde  # type: ignore[method-assign]
    tarea = asyncio.create_task(consumidor.ejecutar(parada))
    await _hasta(lambda: bool(escenario.receptor.pedidos))
    parada.set()
    await tarea
    await _hasta(lambda: escenario.receptor.liberados == [tardio.message_id], limite_s=1)

    assert escenario.manejador.empezados == []
    assert consumidor.en_mano == 0


@pytest.mark.anyio
async def test_sin_concurrencia_la_parada_libera_el_resto_del_lote() -> None:
    escenario = _escenario(pausa_s=0.03)
    escenario.encolar(4)
    consumidor = escenario.consumidor()
    parada = asyncio.Event()

    tarea = asyncio.create_task(consumidor.ejecutar(parada))
    await _hasta(lambda: escenario.manejador.activos == 1)
    parada.set()
    with anyio.fail_after(2):
        await tarea

    assert len(escenario.receptor.borrados) == 1
    assert len(escenario.receptor.liberados) == 3


# --- Causa del fallo (H10) -----------------------------------------------------------------------
@dataclass
class Fallos:
    anotados: list[tuple[str, str | None, Desenlace]] = field(default_factory=list)
    falla: BaseException | None = None

    async def __call__(
        self, mensaje: MensajeSqs, evento: EventoRecibido | None, desenlace: Desenlace
    ) -> None:
        if self.falla is not None:
            raise self.falla
        self.anotados.append(
            (mensaje.message_id, str(evento.event_id) if evento else None, desenlace)
        )


@pytest.mark.anyio
async def test_anota_la_causa_de_cada_desenlace_que_no_borra() -> None:
    escenario = _escenario(falla=ValueError("precipitacion no numérica"))
    (fallido,) = escenario.encolar(1)
    envenenado = _mensaje(cuerpo="[]")
    escenario.receptor.cola.append(envenenado)
    fallos = Fallos()

    assert await escenario.consumidor(concurrencia=2, al_fallar=fallos).ejecutar_una_vez() == 0

    causas = {
        message_id: (evento, d.outcome, d.error_type, d.reason)
        for message_id, evento, d in fallos.anotados
    }
    assert causas == {
        fallido.message_id: (_evento(fallido), inbox.FALLO_TRANSACCION, "ValueError", None),
        envenenado.message_id: (None, inbox.ENVENENADO, None, inbox.MOTIVO_JSON),
    }


@pytest.mark.anyio
async def test_no_anota_los_procesados() -> None:
    escenario = _escenario()
    escenario.encolar(2)
    fallos = Fallos()

    assert await escenario.consumidor(al_fallar=fallos).ejecutar_una_vez() == 2

    assert fallos.anotados == []


@pytest.mark.anyio
async def test_si_anotar_la_causa_falla_lo_registra_y_sigue(telemetria: Any) -> None:
    escenario = _escenario(falla=ValueError("x"))
    (mensaje,) = escenario.encolar(1)

    consumidor = escenario.consumidor(al_fallar=Fallos(falla=ConnectionError("base")))
    assert await consumidor.ejecutar_una_vez() == 0

    errores = [
        r for r in telemetria.logs() if r.get("message") == inbox.MENSAJE_REGISTRO_FALLO_FALLIDO
    ]
    assert [(r["message_id"], r["error_type"]) for r in errores] == [
        (mensaje.message_id, "ConnectionError")
    ]
    (registro,) = _registros(telemetria)
    assert registro["outcome"] == inbox.FALLO_TRANSACCION


# --- Adaptador SQS --------------------------------------------------------------------------------
class ClienteSqs:
    def __init__(self) -> None:
        self.meta = SimpleNamespace(config=SimpleNamespace(read_timeout=25))
        self.recepciones: list[dict[str, Any]] = []
        self.visibilidades: list[dict[str, Any]] = []
        self.hilos: list[int] = []

    def receive_message(self, **kwargs: Any) -> dict[str, Any]:
        self.recepciones.append(kwargs)
        return {}

    def change_message_visibility(self, **kwargs: Any) -> dict[str, Any]:
        self.visibilidades.append(kwargs)
        self.hilos.append(threading.get_ident())
        return {}


@pytest.mark.anyio
@pytest.mark.parametrize(("maximo", "pedido"), [(3, 3), (10, 7), (0, 1)])
async def test_recibir_hasta_no_supera_el_lote_del_receptor(maximo: int, pedido: int) -> None:
    cliente = ClienteSqs()

    await ReceptorSqs(cliente, COLA, max_mensajes=7).recibir_hasta(maximo)

    (recepcion,) = cliente.recepciones
    assert recepcion["MaxNumberOfMessages"] == pedido
    assert recepcion["WaitTimeSeconds"] == inbox.ESPERA_LARGA_S


@pytest.mark.anyio
async def test_extiende_y_libera_por_receipt_handle_en_otro_hilo() -> None:
    cliente = ClienteSqs()
    receptor = ReceptorSqs(cliente, COLA)
    mensaje = MensajeSqs("m-1", "rh-1", "{}", None, 1)

    await receptor.extender(mensaje, 30)
    await receptor.liberar(mensaje)

    assert cliente.visibilidades == [
        {"QueueUrl": COLA, "ReceiptHandle": "rh-1", "VisibilityTimeout": 30},
        {"QueueUrl": COLA, "ReceiptHandle": "rh-1", "VisibilityTimeout": 0},
    ]
    assert threading.get_ident() not in cliente.hilos
