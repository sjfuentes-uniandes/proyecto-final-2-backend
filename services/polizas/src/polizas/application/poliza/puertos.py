"""Puertos de la póliza (HU-W17, 17.6; el documento, 17.7). Todo corre en una transacción `T` recibida: la del inbox
para crearla junto con la proyección del recaudo; la de la tarea emisora (17.8) para el resto."""

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
