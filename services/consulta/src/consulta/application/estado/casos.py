"""Casos de uso de estado y listado de pólizas (HU-W18).

El estado se deriva de la vigencia con la fecha de Bogotá del reloj. Una póliza aún no proyectada
es indistinguible de una inexistente: ambas devuelven `None`.
"""

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from consulta.application.estado.puertos import FiltroPolizas, LecturaEstadoPolizas
from consulta.application.proyeccion.puertos import Reloj
from consulta.domain.polizas.estado import EstadoVigencia, estado_de
from consulta.domain.polizas.modelos import PolizaProyectada, hoy_en_bogota


@dataclass(frozen=True, slots=True)
class EstadoPoliza:
    poliza: PolizaProyectada
    estado: EstadoVigencia


@dataclass(frozen=True, slots=True)
class PaginaPolizas:
    items: tuple[EstadoPoliza, ...]
    total: int
    pagina: int
    tamano: int
    actualizado_en: datetime | None


class ConsultarEstadoPoliza:
    def __init__(self, lectura: LecturaEstadoPolizas, reloj: Reloj, dias_por_vencer: int) -> None:
        self._lectura = lectura
        self._reloj = reloj
        self._dias = dias_por_vencer

    async def ejecutar(self, poliza_id: UUID) -> EstadoPoliza | None:
        poliza = await self._lectura.por_id(poliza_id)
        if poliza is None:
            return None
        hoy = hoy_en_bogota(self._reloj.ahora())
        return EstadoPoliza(poliza, estado_de(poliza.vigencia, hoy, self._dias))


class BuscarPolizas:
    def __init__(self, lectura: LecturaEstadoPolizas, reloj: Reloj, dias_por_vencer: int) -> None:
        self._lectura = lectura
        self._reloj = reloj
        self._dias = dias_por_vencer

    async def ejecutar(
        self,
        *,
        pagina: int,
        tamano: int,
        numero: str | None = None,
        ramo: str | None = None,
        estado: EstadoVigencia | None = None,
        vigencia_desde: date | None = None,
    ) -> PaginaPolizas:
        hoy = hoy_en_bogota(self._reloj.ahora())
        filtro = FiltroPolizas(hoy, self._dias, numero, ramo, estado, vigencia_desde)
        resultado = await self._lectura.buscar(filtro, pagina, tamano)
        items = tuple(
            EstadoPoliza(p, estado_de(p.vigencia, hoy, self._dias)) for p in resultado.polizas
        )
        return PaginaPolizas(items, resultado.total, pagina, tamano, resultado.actualizado_en)
