"""Puertos de la póliza (HU-W17, 17.6; el documento, 17.7; la emisión, 17.8). Todo corre en una
transacción `T` recibida: la del inbox para crearla junto con la proyección del recaudo; la de la
tarea emisora (17.8) para el resto. La lectura de las rutas (17.9) no recibe transacción."""

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from polizas.domain.poliza.documento import DatosDocumento, FuenteDocumento
from polizas.domain.poliza.modelos import OrigenPoliza, Poliza


class PolizasRepositorio[T](Protocol):
    async def origen(self, aceptacion_id: UUID, tx: T) -> OrigenPoliza | None:
        """Lo que la póliza toma de la aceptación y de su decisión; None si polizas no conoce la
        aceptación."""
        ...

    async def siguiente_secuencia(self, tx: T) -> int:
        """El siguiente valor de la secuencia de números. No vuelve atrás con un rollback."""
        ...

    async def crear(self, poliza: Poliza, tx: T) -> bool:
        """Inserta la póliza; False si la aceptación ya tenía una (no cambia nada)."""
        ...

    async def por_aceptacion(self, aceptacion_id: UUID, tx: T) -> Poliza | None: ...

    async def actualizar(self, poliza: Poliza, tx: T) -> bool:
        """Guarda una transición si la póliza sigue EMITIENDO; False si ya no (otra tarea la
        activó)."""
        ...


# --- El documento (HU-W17, 17.7) --------------------------------------------------------------
class FuentesDocumento[T](Protocol):
    async def fuente_documento(self, aceptacion_id: UUID, tx: T) -> FuenteDocumento | None:
        """Lo que el PDF toma de la aceptación, de su decisión y del recaudo confirmado; None si
        falta alguno."""
        ...


class GeneradorDocumento(Protocol):
    def generar(self, datos: DatosDocumento) -> bytes:
        """El PDF: mismos datos, mismos bytes. Síncrono y de CPU (milisegundos)."""
        ...


class AlmacenDocumentos(Protocol):
    """El bucket versionado de los documentos. Lanza `FalloAlmacenDocumentos`."""

    async def guardar(self, clave: str, contenido: bytes) -> str:
        """Sube una versión nueva de `clave` y devuelve su `versionId`."""
        ...

    async def url_descarga(self, clave: str, version_id: str, vence_s: int) -> str:
        """URL prefirmada de esa versión, como adjunto, que vence en `vence_s` segundos."""
        ...


# --- La emisión (HU-W17, 17.8) ----------------------------------------------------------------
class Transacciones[T](Protocol):
    def transaccion(self) -> AbstractAsyncContextManager[T]:
        """Una transacción nueva y corta: commit al salir, rollback si sale con una excepción."""
        ...


class EmisionesRepositorio[T](FuentesDocumento[T], Protocol):
    async def pendientes(
        self, ahora: datetime, reintento_antes_de: datetime, lote: int, tx: T
    ) -> list[Poliza]:
        """Hasta `lote` pólizas EMITIENDO sin una reserva vigente a `ahora`, sin bloquear filas:
        las sin fallo primero, y las fallidas si el fallo es anterior a `reintento_antes_de`."""
        ...

    async def reservar(
        self, poliza_id: UUID, ahora: datetime, hasta: datetime, tx: T
    ) -> Poliza | None:
        """La reserva hasta `hasta` si sigue EMITIENDO y no hay una vigente a `ahora`; devuelve la
        póliza al día, o None si otra tarea la tiene (o ya no está EMITIENDO)."""
        ...

    async def liberar(self, poliza_id: UUID, hasta: datetime, tx: T) -> None:
        """Borra la reserva solo si sigue siendo la propia (la de `hasta`)."""
        ...

    async def por_id(self, poliza_id: UUID, tx: T) -> Poliza | None: ...

    async def actualizar(self, poliza: Poliza, tx: T) -> bool:
        """Guarda la copia si la póliza sigue EMITIENDO y sus puntos de control registrados son
        los de la copia; False si no (otra tarea avanzó), sin cambiar nada."""
        ...


@dataclass(frozen=True, slots=True)
class SolicitudFirma:
    """La firma de la huella del documento: `documento_id` = `polizaId` (la clave idempotente
    hacia el proveedor) y `firmante_ref` = `clienteId`."""

    documento_id: UUID
    huella: str
    firmante_ref: UUID
    correlation_id: str


@dataclass(frozen=True, slots=True)
class FirmaObtenida:
    documento_id: UUID
    huella: str
    firma_id: str
    sello_tiempo: datetime


class FirmaPuerto(Protocol):
    async def firmar(self, solicitud: SolicitudFirma) -> FirmaObtenida:
        """La firma del mismo documento y la misma huella. Lanza `FalloFirma` con su código."""
        ...


class EventosPoliza[T](Protocol):
    async def emitida(
        self, poliza: Poliza, datos: DatosDocumento, correlation_id: str, tx: T
    ) -> None:
        """Escribe `PolizaEmitida` v1 de la póliza ACTIVA en el outbox, dentro de `tx`."""
        ...


# --- La lectura (HU-W17, 17.9) ----------------------------------------------------------------
class LecturaPolizas(Protocol):
    """Las rutas de la póliza leen con el pool, fuera de las transacciones de la tarea emisora."""

    async def por_aceptacion(self, aceptacion_id: UUID) -> Poliza | None: ...

    async def por_id(self, poliza_id: UUID) -> Poliza | None: ...

    async def por_numero(self, numero: str) -> Poliza | None: ...
