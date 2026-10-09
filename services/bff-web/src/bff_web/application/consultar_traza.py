"""Caso de uso: traza de un recorrido por correlationId o la más reciente que cumpla el filtro."""

from collections.abc import Mapping
from typing import Any, Protocol

from solventa_common.telemetry import operacion

from bff_web.domain.trazas import DetalleTraza, FiltroTrazas, construir_detalle

MAX_TRAZAS = 5  # una correlación puede abarcar varias trazas (reintentos, colas)


class RepositorioTrazas(Protocol):
    def buscar(self, filtro: FiltroTrazas, limite: int) -> list[str]:
        """IDs de traza que cumplen el filtro, de la más reciente a la más antigua."""
        ...

    def documentos(self, trace_ids: list[str]) -> list[Mapping[str, Any]]:
        """Documentos de los segmentos de esas trazas."""
        ...


class ConsultarTraza:
    def __init__(self, repositorio: RepositorioTrazas, umbral_ms: int) -> None:
        self.repositorio = repositorio
        self.umbral_ms = umbral_ms

    @operacion("consultar_traza")
    def ejecutar(self, filtro: FiltroTrazas) -> DetalleTraza | None:
        # Sin identificador se muestra solo la traza más reciente que cumpla el filtro.
        limite = MAX_TRAZAS if filtro.correlation_id else 1
        trace_ids = self.repositorio.buscar(filtro, limite)
        if not trace_ids:
            return None
        documentos = self.repositorio.documentos(trace_ids)
        return construir_detalle(trace_ids, documentos, self.umbral_ms, filtro.correlation_id)
