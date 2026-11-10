"""Puertos de la indemnización (HU-W23). El consumidor de `SiniestroAprobado`, la conciliación y la
lectura los usan; las pruebas inyectan dobles.

Las operaciones del repositorio aceptan una transacción (`tx`) que ya abrió otro: la del inbox, para
que la indemnización y la fila del inbox se confirmen juntas. Sin `tx`, cada operación abre la suya.
`Reloj` y `Transacciones` son los del recaudo (HU-W16).
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from dinero.application.recaudo.puertos import Reloj, Transacciones
from dinero.domain.indemnizacion.modelos import Indemnizacion

__all__ = [
    "ConsultaDesembolso",
    "DesembolsoConfirmado",
    "DesembolsoIncierto",
    "DesembolsoNoEncontrado",
    "DesembolsoRechazado",
    "DesembolsosPuerto",
    "EventosIndemnizacion",
    "IndemnizacionesLectura",
    "IndemnizacionesRepositorio",
    "Registro",
    "Reloj",
    "ResultadoConsulta",
    "ResultadoDesembolso",
    "SolicitudDesembolso",
    "Transacciones",
]


@dataclass(frozen=True, slots=True)
class Registro:
    """Resultado de `registrar_pendiente`: la indemnización guardada y si la creó esta llamada.
    Si ya existía, `indemnizacion` es la existente, con el duplicado ya contado."""

    indemnizacion: Indemnizacion
    nuevo: bool


class IndemnizacionesRepositorio[T](Protocol):
    async def registrar_pendiente(
        self, indemnizacion: Indemnizacion, tx: T | None = None
    ) -> Registro:
        """`INSERT … ON CONFLICT (siniestro_id, tipo) DO UPDATE SET duplicados_descartados =
        duplicados_descartados + 1`, sin tocar nada más. Solo quien inserta sale con `nuevo`."""
        ...

    async def registrar_resultado(self, indemnizacion: Indemnizacion, tx: T | None = None) -> int:
        """Escribe el estado y el resultado solo si la fila sigue PENDIENTE. Devuelve las filas
        cambiadas: 0 si otra tarea ya la resolvió."""
        ...

    async def descartar_duplicado(self, indemnizacion_id: UUID, tx: T | None = None) -> None:
        """Suma un duplicado descartado, en cualquier estado."""
        ...

    async def reclamar(
        self, antes_de: datetime, ahora: datetime, hasta: datetime, lote: int
    ) -> list[Indemnizacion]:
        """DS3-18: en una transacción corta, hasta `lote` PENDIENTE con `actualizado_en` anterior a
        `antes_de` y sin reclamo vigente, con `FOR UPDATE SKIP LOCKED`, marcadas con
        `reclamado_hasta = hasta`. Devuelve las reclamadas, las más antiguas primero."""
        ...


class IndemnizacionesLectura(Protocol):
    async def por_siniestro(self, siniestro_id: UUID) -> list[Indemnizacion]: ...


class EventosIndemnizacion[T](Protocol):
    async def agregar(self, indemnizacion: Indemnizacion, tx: T) -> None:
        """Escribe en el outbox, dentro de `tx`, el evento del estado final."""
        ...


# --- Desembolso por adaptador-pagos ------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SolicitudDesembolso:
    """Lo que se envía a `POST /v1/desembolsos`; sin datos de cuenta: el beneficiario es una
    referencia opaca que nunca va a los logs ni a los eventos."""

    referencia: str
    monto: Decimal
    moneda: str
    beneficiario_ref: str
    correlation_id: str


@dataclass(frozen=True, slots=True)
class DesembolsoConfirmado:
    transaccion_id: str


@dataclass(frozen=True, slots=True)
class DesembolsoRechazado:
    # Tal como lo informa la pasarela: el dominio lo normaliza a un código.
    motivo: str | None
    transaccion_id: str | None


@dataclass(frozen=True, slots=True)
class DesembolsoIncierto:
    """Sin respuesta definitiva (timeout, 5xx, respuesta inválida u otro estado): nunca un
    rechazo. `codigo` tiene forma de código."""

    codigo: str


type ResultadoDesembolso = DesembolsoConfirmado | DesembolsoRechazado | DesembolsoIncierto


@dataclass(frozen=True, slots=True)
class ConsultaDesembolso:
    """`GET /v1/desembolsos/{referencia}?beneficiarioRef`: la pasarela elige el caso por el
    beneficiario. Nunca va a los logs."""

    referencia: str
    beneficiario_ref: str
    correlation_id: str


@dataclass(frozen=True, slots=True)
class DesembolsoNoEncontrado:
    """`404 DESEMBOLSO_NO_ENCONTRADO`: la orden nunca llegó a la pasarela. No es un rechazo."""


type ResultadoConsulta = ResultadoDesembolso | DesembolsoNoEncontrado


class DesembolsosPuerto(Protocol):
    async def desembolsar(self, solicitud: SolicitudDesembolso) -> ResultadoDesembolso:
        """No lanza por fallos del adaptador: los devuelve como `DesembolsoIncierto`."""
        ...

    async def consultar_desembolso(self, consulta: ConsultaDesembolso) -> ResultadoConsulta:
        """Como `desembolsar`: no lanza por fallos del adaptador."""
        ...
