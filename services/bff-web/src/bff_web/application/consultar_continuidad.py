"""Casos de uso: continuidad del servicio (I04) y conciliación del incidente (I05), HU-W29."""

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from solventa_common.telemetry import operacion

from bff_web.domain.alertas import Transicion
from bff_web.domain.continuidad import (
    Continuidad,
    EstadoTarea,
    base,
    cronologia,
    estado_general,
    indicadores,
    inicio_incidente,
    recorridos,
    zonas,
)

logger = logging.getLogger("bff_web.continuidad")
VENTANA_TRAFICO = timedelta(minutes=5)


class RepositorioEventos(Protocol):
    def tareas(self) -> list[EstadoTarea]: ...

    def eventos(self, desde: datetime, hasta: datetime) -> list[Mapping[str, Any]]: ...


class RepositorioTrafico(Protocol):
    def por_zona(self, desde: datetime, hasta: datetime) -> dict[str, float]: ...


class RepositorioAlarmas(Protocol):
    def abiertas(self, prefijo: str) -> list[Mapping[str, Any]]: ...

    def transiciones(self, prefijo: str, desde: datetime, hasta: datetime) -> list[Transicion]: ...


class RepositorioInformes(Protocol):
    def ultimo(self) -> Mapping[str, Any] | None: ...


class ConsultarContinuidad:
    def __init__(
        self,
        eventos: RepositorioEventos,
        trafico: RepositorioTrafico,
        alarmas: RepositorioAlarmas,
        informes: RepositorioInformes,
        prefijo: str,
        recorridos: Mapping[str, Mapping[str, Any]],
        titulos: Mapping[str, str],
        ahora: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.eventos = eventos
        self.trafico = trafico
        self.alarmas = alarmas
        self.informes = informes
        self.prefijo = prefijo
        self.recorridos = recorridos
        self.titulos = titulos
        self.ahora = ahora

    @operacion("consultar_continuidad")
    def ejecutar(self, horas: int) -> Continuidad:
        ahora = self.ahora()
        desde = ahora - timedelta(hours=horas)
        eventos = self.eventos.eventos(desde, ahora)
        lista = zonas(self.eventos.tareas(), self.trafico.por_zona(ahora - VENTANA_TRAFICO, ahora))
        abiertas = [str(a["AlarmName"]) for a in self.alarmas.abiertas(self.prefijo)]
        hitos = cronologia(
            eventos,
            self.alarmas.transiciones(self.prefijo, desde, ahora),
            self.titulos,
            self.prefijo,
        )
        return Continuidad(
            estado=estado_general(lista),
            zonas=lista,
            base=base(eventos),
            recorridos=recorridos(self.recorridos, abiertas, self.prefijo),
            cronologia=hitos,
            incidente_desde=inicio_incidente(hitos),
            indicadores=indicadores(self._informe()),
        )

    def _informe(self) -> Mapping[str, Any] | None:
        # Sin informe (o sin bucket fuera de exp) la pantalla sigue: los indicadores quedan vacíos.
        try:
            return self.informes.ultimo()
        except Exception as error:  # noqa: BLE001 - el informe es complementario
            logger.warning(
                "no se pudo leer el informe de conciliación",
                extra={"error_type": type(error).__name__},
            )
            return None


class ConsultarConciliacion:
    def __init__(self, informes: RepositorioInformes) -> None:
        self.informes = informes

    @operacion("consultar_conciliacion")
    def ejecutar(self) -> Mapping[str, Any] | None:
        return self.informes.ultimo()
