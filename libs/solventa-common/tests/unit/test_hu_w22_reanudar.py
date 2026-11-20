"""HU-W22 (UT-W22-02, N1): el gancho `reanudar` del consumidor común (DS3-15).

`reanudar(evento) -> Continuacion | None` se llama cuando el `eventId` ya estaba en el inbox, fuera
de la transacción, y su continuación corre antes de borrar. Cada caso se prueba en el camino
secuencial (HU-W16) y en el concurrente con latido de visibilidad (HU-W24). Sin `reanudar`, el
comportamiento de HU-W16 y HU-W24 no cambia (`test_inbox.py` y `test_hu_w24_consumidor.py` siguen
sin tocarse). Las pruebas con PostgreSQL y LocalStack reales: `tests/module/test_hu_w22_reanudar.py`.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import anyio
import pytest
from solventa_common import inbox
from solventa_common.inbox import (
    ConsumidorInbox,
    Continuacion,
    Desenlace,
    EventoRecibido,
    MensajeSqs,
)

INSTANTE = datetime(2026, 11, 19, 14, 0, tzinfo=UTC)
CAMINOS = ["secuencial", "concurrente"]


def _mensaje(event_id: UUID | None = None) -> MensajeSqs:
    sobre = {
        "eventId": str(event_id or uuid4()),
        "aggregateId": str(uuid4()),
        "type": "EventoParametricoObservado",
        "version": 1,
        "occurredAt": "2026-11-19T14:00:00Z",
        "correlationId": "corr-w22-01",
        "payload": {"eventoId": str(uuid4())},
    }
    return MensajeSqs(
        message_id=f"msg-{uuid4().hex[:8]}",
        receipt_handle=f"rh-{uuid4().hex}",
        cuerpo=json.dumps(sobre),
        tipo_atributo="EventoParametricoObservado",
        recepciones=1,
    )


# --- Puertos falsos -------------------------------------------------------------------------
@dataclass
class Tx:
    repo: "Repo"
    nuevos: set[UUID] = field(default_factory=set)

    async def registrar(self, event_id: UUID, event_type: str, recibido_en: datetime) -> bool:
        if event_id in self.repo.inbox or event_id in self.nuevos:
            return False
        self.nuevos.add(event_id)
        return True


@dataclass
class Repo:
    bitacora: list[str]
    inbox: set[UUID] = field(default_factory=set)
    abiertas: int = 0

    @asynccontextmanager
    async def transaccion(self) -> AsyncIterator[Tx]:
        tx = Tx(self)
        self.abiertas += 1
        try:
            yield tx
        except BaseException:
            self.bitacora.append("rollback")
            raise
        finally:
            self.abiertas -= 1
        self.inbox |= tx.nuevos
        self.bitacora.append("commit")


@dataclass
class Receptor:
    """Una cola con los dos contratos: `recibir` (secuencial) y el controlado (concurrente)."""

    bitacora: list[str]
    cola: list[MensajeSqs] = field(default_factory=list)
    borrados: list[str] = field(default_factory=list)
    extensiones: list[tuple[str, int]] = field(default_factory=list)

    async def recibir(self) -> list[MensajeSqs]:
        return await self.recibir_hasta(10)

    async def recibir_hasta(self, maximo: int) -> list[MensajeSqs]:
        if not self.cola:
            await asyncio.sleep(0.005)
            return []
        lote, self.cola = self.cola[:maximo], self.cola[maximo:]
        return lote

    async def borrar(self, mensaje: MensajeSqs) -> None:
        self.bitacora.append("borrar")
        self.borrados.append(mensaje.message_id)

    async def extender(self, mensaje: MensajeSqs, segundos: int) -> None:
        self.extensiones.append((mensaje.message_id, segundos))

    async def liberar(self, mensaje: MensajeSqs) -> None:
        return None


@dataclass
class Servicio:
    """El manejador (modo a o b) y el `reanudar` del servicio, con lo que cada uno hizo."""

    bitacora: list[str]
    repo: Repo
    pendiente: bool = True
    continuacion_del_manejador: bool = False
    falla_continuacion: BaseException | None = None
    falla_reanudar: BaseException | None = None
    pausa_continuacion_s: float = 0
    manejados: int = 0
    reanudados: int = 0
    continuaciones: int = 0
    transacciones_abiertas_al_reanudar: list[int] = field(default_factory=list)
    fallos: list[Desenlace] = field(default_factory=list)

    async def manejar(self, evento: EventoRecibido, tx: Tx) -> Continuacion | None:
        self.bitacora.append("manejador")
        self.manejados += 1
        if not self.continuacion_del_manejador:
            return None
        return self._continuar

    async def _continuar(self) -> None:
        self.bitacora.append("continuacion")
        self.continuaciones += 1
        if self.pausa_continuacion_s:
            await asyncio.sleep(self.pausa_continuacion_s)
        if self.falla_continuacion is not None:
            raise self.falla_continuacion
        self.pendiente = False

    async def reanudar(self, evento: EventoRecibido) -> Continuacion | None:
        self.bitacora.append("reanudar")
        self.reanudados += 1
        self.transacciones_abiertas_al_reanudar.append(self.repo.abiertas)
        if self.falla_reanudar is not None:
            raise self.falla_reanudar
        return self._continuar if self.pendiente else None

    async def al_fallar(
        self, mensaje: MensajeSqs, evento: EventoRecibido | None, desenlace: Desenlace
    ) -> None:
        self.fallos.append(desenlace)


@dataclass
class Escenario:
    camino: str
    bitacora: list[str]
    receptor: Receptor
    repo: Repo
    servicio: Servicio

    def consumidor(self, *, con_reanudar: bool = True, **kwargs: Any) -> ConsumidorInbox[Tx]:
        if self.camino == "concurrente":
            kwargs = {"concurrencia": 2, "visibilidad_s": 30, "latido_s": 0.01, **kwargs}
        return ConsumidorInbox(
            self.receptor,
            self.repo,
            self.servicio.manejar,
            reloj=lambda: INSTANTE,
            al_fallar=self.servicio.al_fallar,
            reanudar=self.servicio.reanudar if con_reanudar else None,
            **kwargs,
        )

    def entregar(self, *mensajes: MensajeSqs) -> None:
        self.receptor.cola.extend(mensajes)


def _escenario(camino: str, **servicio: Any) -> Escenario:
    bitacora: list[str] = []
    repo = Repo(bitacora)
    return Escenario(
        camino, bitacora, Receptor(bitacora), repo, Servicio(bitacora, repo, **servicio)
    )


def _registros(telemetria: Any) -> list[dict[str, Any]]:
    return [
        r
        for r in telemetria.logs()
        if r.get("operation") == inbox.OPERACION and r["logger"] == "solventa.telemetria"
    ]


# --- Sin `reanudar`: nada cambia ------------------------------------------------------------
@pytest.mark.anyio
@pytest.mark.parametrize("camino", CAMINOS)
async def test_sin_reanudar_un_duplicado_se_borra_aunque_la_continuacion_haya_fallado(
    camino: str,
) -> None:
    escenario = _escenario(
        camino, continuacion_del_manejador=True, falla_continuacion=TimeoutError()
    )
    mensaje = _mensaje()
    consumidor = escenario.consumidor(con_reanudar=False)

    escenario.entregar(mensaje)
    assert await consumidor.ejecutar_una_vez() == 0  # la continuación falló: no se borra
    escenario.bitacora.clear()
    escenario.entregar(mensaje)
    assert await consumidor.ejecutar_una_vez() == 1  # la reentrega es un duplicado: se borra

    assert escenario.bitacora == ["commit", "borrar"]
    assert escenario.servicio.continuaciones == 1  # la continuación no se repitió
    assert escenario.servicio.reanudados == 0


# --- Con `reanudar` -------------------------------------------------------------------------
@pytest.mark.anyio
@pytest.mark.parametrize("camino", CAMINOS)
async def test_un_evento_nuevo_no_llama_a_reanudar(camino: str) -> None:
    escenario = _escenario(camino)
    escenario.entregar(_mensaje())

    assert await escenario.consumidor().ejecutar_una_vez() == 1

    assert escenario.servicio.manejados == 1
    assert escenario.servicio.reanudados == 0
    assert escenario.bitacora == ["manejador", "commit", "borrar"]


@pytest.mark.anyio
@pytest.mark.parametrize("camino", CAMINOS)
async def test_la_continuacion_que_funciona_corre_fuera_de_la_transaccion_y_antes_de_borrar(
    camino: str, telemetria: Any
) -> None:
    escenario = _escenario(camino, pendiente=True)
    mensaje = _mensaje()
    escenario.repo.inbox.add(json_event_id(mensaje))
    escenario.entregar(mensaje)

    assert await escenario.consumidor().ejecutar_una_vez() == 1

    # El manejador de W21 no se vuelve a llamar; `reanudar` va tras el commit del inbox, con
    # ninguna transacción abierta, y la continuación antes del borrado.
    assert escenario.bitacora == ["commit", "reanudar", "continuacion", "borrar"]
    assert escenario.servicio.manejados == 0
    assert escenario.servicio.transacciones_abiertas_al_reanudar == [0]
    assert escenario.receptor.borrados == [mensaje.message_id]
    (registro,) = _registros(telemetria)
    assert registro["outcome"] == inbox.REANUDADO
    assert registro["result"] == "ok"


@pytest.mark.anyio
@pytest.mark.parametrize("camino", CAMINOS)
async def test_si_la_continuacion_reanudada_falla_no_borra_y_la_siguiente_recepcion_reintenta(
    camino: str, telemetria: Any
) -> None:
    escenario = _escenario(camino, falla_continuacion=TimeoutError())
    mensaje = _mensaje()
    escenario.repo.inbox.add(json_event_id(mensaje))
    consumidor = escenario.consumidor()

    for _ in range(3):  # tres recepciones del mismo mensaje con siniestros caído
        escenario.entregar(mensaje)
        assert await consumidor.ejecutar_una_vez() == 0
    assert escenario.receptor.borrados == []
    assert escenario.servicio.continuaciones == 3

    escenario.servicio.falla_continuacion = None  # siniestros de vuelta
    escenario.entregar(mensaje)
    assert await consumidor.ejecutar_una_vez() == 1

    assert escenario.servicio.continuaciones == 4
    assert escenario.receptor.borrados == [mensaje.message_id]
    resultados = [r["outcome"] for r in _registros(telemetria)]
    assert resultados == [inbox.FALLO_CONTINUACION] * 3 + [inbox.REANUDADO]
    assert escenario.servicio.fallos == [Desenlace(inbox.FALLO_CONTINUACION, "TimeoutError")] * 3


@pytest.mark.anyio
@pytest.mark.parametrize("camino", CAMINOS)
async def test_si_reanudar_lanza_no_borra(camino: str) -> None:
    escenario = _escenario(camino, falla_reanudar=ConnectionError("base caída"))
    mensaje = _mensaje()
    escenario.repo.inbox.add(json_event_id(mensaje))
    escenario.entregar(mensaje)

    assert await escenario.consumidor().ejecutar_una_vez() == 0

    assert escenario.receptor.borrados == []
    assert escenario.servicio.continuaciones == 0
    assert escenario.servicio.fallos == [Desenlace(inbox.FALLO_CONTINUACION, "ConnectionError")]


@pytest.mark.anyio
@pytest.mark.parametrize("camino", CAMINOS)
async def test_si_no_queda_nada_pendiente_es_un_duplicado_y_se_borra(
    camino: str, telemetria: Any
) -> None:
    escenario = _escenario(camino, pendiente=False)
    mensaje = _mensaje()
    escenario.repo.inbox.add(json_event_id(mensaje))
    escenario.entregar(mensaje)

    assert await escenario.consumidor().ejecutar_una_vez() == 1

    assert escenario.bitacora == ["commit", "reanudar", "borrar"]
    assert escenario.servicio.continuaciones == 0
    (registro,) = _registros(telemetria)
    assert registro["outcome"] == inbox.DUPLICADO


@pytest.mark.anyio
@pytest.mark.parametrize("camino", CAMINOS)
async def test_primer_intento_fallido_y_reanudacion_completan_una_sola_vez(camino: str) -> None:
    """El modo (b) de W22: la continuación del primer intento falla, el mensaje vuelve y `reanudar`
    la completa; el manejador transaccional corrió una sola vez."""
    escenario = _escenario(
        camino, continuacion_del_manejador=True, falla_continuacion=TimeoutError()
    )
    mensaje = _mensaje()
    consumidor = escenario.consumidor()

    escenario.entregar(mensaje)
    assert await consumidor.ejecutar_una_vez() == 0
    escenario.servicio.falla_continuacion = None
    escenario.entregar(mensaje)
    assert await consumidor.ejecutar_una_vez() == 1

    assert escenario.servicio.manejados == 1
    assert escenario.servicio.continuaciones == 2
    assert escenario.receptor.borrados == [mensaje.message_id]


@pytest.mark.anyio
async def test_en_el_camino_concurrente_la_continuacion_reanudada_tiene_el_latido() -> None:
    escenario = _escenario("concurrente", pausa_continuacion_s=0.08)
    mensaje = _mensaje()
    escenario.repo.inbox.add(json_event_id(mensaje))
    escenario.entregar(mensaje)
    consumidor = escenario.consumidor()
    parada = asyncio.Event()

    async def detener() -> None:
        with anyio.fail_after(2):
            while not escenario.receptor.borrados:
                await asyncio.sleep(0.005)
        parada.set()

    with anyio.fail_after(3):
        await asyncio.gather(consumidor.ejecutar(parada), detener())

    # Mientras la continuación esperaba a `siniestros`, el latido renovó la visibilidad.
    assert escenario.receptor.extensiones
    assert {m for m, _ in escenario.receptor.extensiones} == {mensaje.message_id}
    assert escenario.servicio.continuaciones == 1


@pytest.mark.anyio
async def test_dos_consumidores_con_el_mismo_mensaje_pueden_reanudar_a_la_vez() -> None:
    """La continuación debe ser idempotente: las dos reanudaciones corren y las dos borran."""
    escenario = _escenario("concurrente", pausa_continuacion_s=0.02)
    mensaje = _mensaje()
    escenario.repo.inbox.add(json_event_id(mensaje))
    primero, segundo = _mensaje(json_event_id(mensaje)), _mensaje(json_event_id(mensaje))
    escenario.entregar(primero, segundo)

    assert await escenario.consumidor().ejecutar_una_vez() == 2

    assert escenario.servicio.continuaciones == 2
    assert escenario.receptor.borrados == [primero.message_id, segundo.message_id]


def json_event_id(mensaje: MensajeSqs) -> UUID:
    return UUID(json.loads(mensaje.cuerpo)["eventId"])


def test_el_desenlace_reanudado_cuenta_como_procesado() -> None:
    assert Desenlace(inbox.REANUDADO).procesado
    assert not Desenlace(inbox.FALLO_CONTINUACION, "TimeoutError").procesado


def test_reanudar_es_opcional_y_por_omision_none() -> None:
    escenario = _escenario("secuencial")
    consumidor: Callable[..., Any] = ConsumidorInbox
    # El constructor admite el parámetro por nombre y no lo exige.
    assert consumidor(escenario.receptor, escenario.repo, escenario.servicio.manejar) is not None
