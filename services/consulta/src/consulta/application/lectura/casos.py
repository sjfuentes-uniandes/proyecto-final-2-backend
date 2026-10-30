"""Casos de uso de lectura de pólizas del cliente (HU-M10).

`vigente` se calcula al leer con la fecha de Bogotá del reloj; no se guarda.
"""

from dataclasses import dataclass
from uuid import UUID

from consulta.application.lectura.puertos import (
    LecturaPolizas,
    PolizaNoAutorizada,
    PolizaNoEncontrada,
)
from consulta.application.proyeccion.puertos import Reloj
from consulta.domain.polizas.modelos import PolizaProyectada, es_vigente, hoy_en_bogota


@dataclass(frozen=True, slots=True)
class PolizaConsultada:
    poliza: PolizaProyectada
    vigente: bool


class ListarPolizas:
    def __init__(self, lectura: LecturaPolizas, reloj: Reloj) -> None:
        self._lectura = lectura
        self._reloj = reloj

    async def ejecutar(self, cliente_id: UUID) -> list[PolizaConsultada]:
        hoy = hoy_en_bogota(self._reloj.ahora())
        return [
            PolizaConsultada(p, es_vigente(p.vigencia, hoy))
            for p in await self._lectura.por_cliente(cliente_id)
        ]


class ConsultarPoliza:
    def __init__(self, lectura: LecturaPolizas, reloj: Reloj) -> None:
        self._lectura = lectura
        self._reloj = reloj

    async def ejecutar(self, cliente_id: UUID, poliza_id: UUID) -> PolizaConsultada:
        poliza = await self._lectura.por_id(poliza_id)
        if poliza is None:
            raise PolizaNoEncontrada
        if poliza.cliente_id != cliente_id:
            raise PolizaNoAutorizada
        hoy = hoy_en_bogota(self._reloj.ahora())
        return PolizaConsultada(poliza, es_vigente(poliza.vigencia, hoy))
