"""Caso de uso: cotizaciones por minuto y capacidad de cotizacion en la última hora (HU-W08).

La ventana termina en el último minuto completo (el minuto en curso aún no tiene todos sus
datos) y dura VENTANA_MIN minutos (plan de W08, decisión 14).
"""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from solventa_common.telemetry import operacion

from bff_web.domain.capacidad import (
    PanelCapacidad,
    al_minuto,
    serie_por_minuto,
    ultimo_escalado,
    ultimo_valor,
)

VENTANA_MIN = 60


class RepositorioCapacidad(Protocol):
    servicio: str

    def metricas(
        self, desde: datetime, hasta: datetime
    ) -> tuple[dict[datetime, float], dict[datetime, float]]: ...

    def limites(self) -> tuple[int, int]: ...

    def actividades(self, desde: datetime) -> list[Mapping[str, Any]]: ...


class ConsultarCapacidad:
    def __init__(
        self,
        repositorio: RepositorioCapacidad,
        ventana_min: int = VENTANA_MIN,
        ahora: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repositorio = repositorio
        self.ventana_min = ventana_min
        self.ahora = ahora

    @operacion("consultar_capacidad")
    def ejecutar(self) -> PanelCapacidad:
        hasta = al_minuto(self.ahora())
        desde = hasta - timedelta(minutes=self.ventana_min)
        cotizaciones, tareas = self.repositorio.metricas(desde, hasta)
        minimo, maximo = self.repositorio.limites()
        return PanelCapacidad(
            servicio=self.repositorio.servicio,
            ventana_min=self.ventana_min,
            cotizaciones_por_minuto=serie_por_minuto(cotizaciones, desde, self.ventana_min),
            tareas_activas=ultimo_valor(tareas),
            minimo=minimo,
            maximo=maximo,
            ultimo_escalado=ultimo_escalado(self.repositorio.actividades(desde), desde),
        )
