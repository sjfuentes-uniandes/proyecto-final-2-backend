"""Puertos del recaudo (HU-W16). El consumidor de `OfertaAceptada` (16.10) y la conciliación (16.11)
los usan; las pruebas inyectan dobles.

Las operaciones del repositorio aceptan una transacción (`tx`) que ya abrió otro: la del inbox, para
que el movimiento y la fila del inbox se confirmen juntos. Sin `tx`, cada operación abre la suya.
"""

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from dinero.domain.recaudo.modelos import Recaudo


class Reloj(Protocol):
    def ahora(self) -> datetime:
        """Instante actual en UTC."""
        ...


@dataclass(frozen=True, slots=True)
class Creacion:
    """Resultado de `crear_pendiente`: el movimiento guardado y si lo creó esta llamada."""

    recaudo: Recaudo
    nuevo: bool


class RegistroResultados[T](Protocol):
    async def registrar_resultado(self, recaudo: Recaudo, tx: T | None = None) -> int:
        """Escribe el estado y el resultado de `recaudo` solo si la fila sigue PENDIENTE. Devuelve
        las filas cambiadas: 0 si otra tarea ya lo resolvió."""
        ...


class RecaudosRepositorio[T](RegistroResultados[T], Protocol):
    async def crear_pendiente(self, recaudo: Recaudo, tx: T | None = None) -> Creacion:
        """Inserta el movimiento PENDIENTE. Si ya hay uno con su `claveRecaudo`, no inserta nada y
        devuelve el existente con `nuevo = False`."""
        ...

    async def por_clave(self, clave_recaudo: UUID, tx: T | None = None) -> Recaudo | None: ...


class PendientesRepositorio[T](RegistroResultados[T], Protocol):
    async def pendientes(self, antes_de: datetime, lote: int) -> list[Recaudo]:
        """Hasta `lote` movimientos PENDIENTE con `actualizado_en` anterior a `antes_de`, los más
        antiguos primero. Sin bloquear filas: la guarda de `registrar_resultado` basta (16.11)."""
        ...


class Transacciones[T](Protocol):
    def transaccion(self) -> AbstractAsyncContextManager[T]:
        """Una transacción nueva: commit al salir, rollback si sale con una excepción."""
        ...


class EventosRecaudo[T](Protocol):
    async def agregar(self, recaudo: Recaudo, tx: T) -> None:
        """Escribe en el outbox, dentro de `tx`, el evento del estado final de `recaudo`."""
        ...


# --- Cobro por adaptador-pagos ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SolicitudCobro:
    """Lo que se envía a `POST /v1/cobros`; sin datos de tarjeta ni de cuenta (ADR 0013, 6)."""

    clave_recaudo: UUID
    monto: Decimal
    moneda: str
    # Pagador sintético (`clienteId` de OfertaAceptada): nunca va a los logs ni a los eventos.
    pagador_ref: UUID
    correlation_id: str


@dataclass(frozen=True, slots=True)
class CobroAprobado:
    transaccion_id: str


@dataclass(frozen=True, slots=True)
class CobroRechazado:
    # Tal como lo informa el proveedor: el dominio lo normaliza a un código.
    motivo: str | None
    transaccion_id: str | None


@dataclass(frozen=True, slots=True)
class CobroIncierto:
    """Sin respuesta definitiva (timeout, 5xx, respuesta inválida u otro estado): nunca un
    rechazo. `codigo` tiene forma de código."""

    codigo: str


type ResultadoCobro = CobroAprobado | CobroRechazado | CobroIncierto


# --- Consulta por adaptador-pagos (conciliación, 16.11) ---------------------------------------------
@dataclass(frozen=True, slots=True)
class ConsultaCobro:
    """Lo que se envía a `GET /v1/cobros/{claveRecaudo}?pagadorRef`: el proveedor elige el caso por
    el pagador, así que la consulta lo necesita. Nunca va a los logs."""

    clave_recaudo: UUID
    pagador_ref: UUID
    correlation_id: str


@dataclass(frozen=True, slots=True)
class CobroNoEncontrado:
    """`404 COBRO_NO_ENCONTRADO`: el cobro nunca llegó al proveedor. No es un rechazo (P16-3)."""


type ResultadoConsulta = ResultadoCobro | CobroNoEncontrado


class PagosPuerto(Protocol):
    async def cobrar(self, solicitud: SolicitudCobro) -> ResultadoCobro:
        """No lanza por fallos del adaptador: los devuelve como `CobroIncierto`."""
        ...

    async def consultar(self, consulta: ConsultaCobro) -> ResultadoConsulta:
        """Como `cobrar`: no lanza por fallos del adaptador."""
        ...
