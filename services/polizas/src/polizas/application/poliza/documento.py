"""Reúne los datos del PDF de una póliza EMITIENDO (HU-W17, 17.7; ADR 0014, decisión 4), en la
transacción recibida: la de la tarea emisora (17.8). Sin llamadas a otros servicios: todo está en
la aceptación, en la instantánea de su decisión (DS2-10) y en la proyección del recaudo."""

from datetime import datetime

from polizas.application.poliza.puertos import FuentesDocumento
from polizas.domain.poliza.documento import DatosDocumento, datos_documento
from polizas.domain.poliza.errores import DatosDocumentoNoDisponibles
from polizas.domain.poliza.modelos import Poliza


class ReunirDatosDocumento[T]:
    def __init__(self, fuentes: FuentesDocumento[T]) -> None:
        self._fuentes = fuentes

    async def ejecutar(self, poliza: Poliza, fecha_documento: datetime, tx: T) -> DatosDocumento:
        """Lanza `DatosDocumentoNoDisponibles` si falta la aceptación, su decisión o el recaudo
        confirmado (no pasa con una póliza creada desde `RecaudoConfirmado`)."""
        fuente = await self._fuentes.fuente_documento(poliza.aceptacion_id, tx)
        if fuente is None:
            raise DatosDocumentoNoDisponibles()
        return datos_documento(poliza, fuente, fecha_documento)
