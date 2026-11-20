"""Leer las indemnizaciones de un siniestro (HU-W23, W23-B13): `GET /v1/indemnizaciones`."""

from uuid import UUID

from solventa_common.telemetry import RESULTADO_OK, operacion

from dinero.application.indemnizacion.puertos import IndemnizacionesLectura
from dinero.domain.indemnizacion.modelos import Indemnizacion

OPERACION = "indemnizacion.consultar"


class ConsultarIndemnizaciones:
    def __init__(self, lectura: IndemnizacionesLectura) -> None:
        self._lectura = lectura

    async def ejecutar(self, siniestro_id: UUID) -> list[Indemnizacion]:
        """Como mucho una por `(siniestro_id, tipo)`; vacía si el siniestro aún no tiene pago."""
        with operacion(OPERACION) as medicion:
            encontradas = await self._lectura.por_siniestro(siniestro_id)
            medicion.marcar(RESULTADO_OK)
            return encontradas
