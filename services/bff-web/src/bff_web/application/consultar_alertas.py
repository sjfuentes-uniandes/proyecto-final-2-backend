"""Caso de uso: alertas abiertas, umbrales configurados y resumen de los últimos 30 días (HU-W28)."""

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from solventa_common.telemetry import operacion

from bff_web.domain.alertas import (
    AlertaAbierta,
    EntradaCatalogo,
    PanelAlertas,
    Transicion,
    Umbral,
    alerta_abierta,
    ordenar,
    resumir,
)

PERIODO_RESUMEN = timedelta(days=30)  # retención del historial de alarmas


class RepositorioAlarmas(Protocol):
    def abiertas(self, prefijo: str) -> list[Mapping[str, Any]]: ...

    def transiciones(self, prefijo: str, desde: datetime, hasta: datetime) -> list[Transicion]: ...

    def socio_con_mayor_cuota(self) -> str | None: ...


class ConsultarAlertas:
    def __init__(
        self,
        repositorio: RepositorioAlarmas,
        prefijo: str,
        catalogo: Mapping[str, EntradaCatalogo],
        umbrales: list[Umbral],
        falso_positivo_min: float,
        atencion_min: float,
        ahora: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repositorio = repositorio
        self.prefijo = prefijo
        self.catalogo = catalogo
        self.umbrales = umbrales
        self.falso_positivo_min = falso_positivo_min
        self.atencion_min = atencion_min
        self.ahora = ahora

    @operacion("consultar_alertas")
    def ejecutar(self) -> PanelAlertas:
        ahora = self.ahora()
        desde = ahora - PERIODO_RESUMEN
        abiertas = [
            a for a in (alerta_abierta(alarma, self.prefijo, self.catalogo) for alarma in self.repositorio.abiertas(self.prefijo))
            if a is not None
        ]
        transiciones = self.repositorio.transiciones(self.prefijo, desde, ahora)
        return PanelAlertas(
            abiertas=ordenar(self._con_socio(a) for a in abiertas),
            umbrales=self.umbrales,
            resumen=resumir(transiciones, self.catalogo, self.prefijo, desde, ahora, self.falso_positivo_min, self.atencion_min),
        )

    def _con_socio(self, alerta: AlertaAbierta) -> AlertaAbierta:
        if alerta.enlace.get("tipo") != "socio":
            return alerta
        socio_id = self.repositorio.socio_con_mayor_cuota()
        return replace(alerta, enlace={**alerta.enlace, "socio_id": socio_id}) if socio_id else alerta
