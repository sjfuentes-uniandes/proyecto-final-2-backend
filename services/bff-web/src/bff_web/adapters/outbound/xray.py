"""Repositorio de trazas sobre AWS X-Ray (GetTraceSummaries + BatchGetTraces).

El colector ADOT indexa `correlation_id` y `operation` como anotaciones
(infra/apps/ecs.tf), por eso se puede filtrar con annotation.correlation_id.
"""

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from solventa_common.correlation import es_valido

from bff_web.domain.trazas import ENTRADAS, FiltroTrazas

VENTANA = timedelta(hours=6)  # se consulta por ventanas para cubrir hasta 24 h
MAX_PAGINAS = 10
LOTE_BATCH_GET = 5


def expresion_filtro(filtro: FiltroTrazas) -> str | None:
    """Expresión de filtro de X-Ray. Solo admite valores validados (sin inyección)."""
    partes = []
    if filtro.correlation_id is not None:
        if not es_valido(filtro.correlation_id):
            raise ValueError("correlation_id inválido")
        partes.append(f'annotation.correlation_id = "{filtro.correlation_id}"')
    if filtro.entrada is not None:
        if filtro.entrada not in ENTRADAS:
            raise ValueError("entrada inválida")
        partes.append(f'service("{filtro.entrada}")')
    if filtro.duracion_min_ms > 0:
        partes.append(f"duration >= {filtro.duracion_min_ms / 1000:.3f}")
    return " AND ".join(partes) or None


class RepositorioXRay:
    def __init__(self, cliente: Any, ahora: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.cliente = cliente
        self.ahora = ahora

    def _resumenes(self, inicio: datetime, fin: datetime, expresion: str | None) -> list[Mapping[str, Any]]:
        parametros: dict[str, Any] = {"StartTime": inicio, "EndTime": fin, "Sampling": False}
        if expresion:
            parametros["FilterExpression"] = expresion
        resumenes: list[Mapping[str, Any]] = []
        for _ in range(MAX_PAGINAS):
            respuesta = self.cliente.get_trace_summaries(**parametros)
            resumenes.extend(respuesta.get("TraceSummaries", []))
            if not respuesta.get("NextToken"):
                break
            parametros["NextToken"] = respuesta["NextToken"]
        return resumenes

    def buscar(self, filtro: FiltroTrazas, limite: int) -> list[str]:
        expresion = expresion_filtro(filtro)
        fin = self.ahora()
        limite_inferior = fin - timedelta(hours=filtro.horas)
        encontrados: list[str] = []
        while fin > limite_inferior and len(encontrados) < limite:
            inicio = max(fin - VENTANA, limite_inferior)
            resumenes = self._resumenes(inicio, fin, expresion)
            if filtro.entrada:
                # service() incluye trazas que solo pasan por el servicio; se exige que entren por él.
                resumenes = [r for r in resumenes if (r.get("EntryPoint") or {}).get("Name") == filtro.entrada]
            resumenes = sorted(resumenes, key=lambda r: r.get("StartTime") or fin, reverse=True)
            encontrados.extend(str(r["Id"]) for r in resumenes if r["Id"] not in encontrados)
            fin = inicio
        return encontrados[:limite]

    def documentos(self, trace_ids: list[str]) -> list[Mapping[str, Any]]:
        documentos: list[Mapping[str, Any]] = []
        for i in range(0, len(trace_ids), LOTE_BATCH_GET):
            parametros: dict[str, Any] = {"TraceIds": trace_ids[i : i + LOTE_BATCH_GET]}
            for _ in range(MAX_PAGINAS):
                respuesta = self.cliente.batch_get_traces(**parametros)
                for traza in respuesta.get("Traces", []):
                    documentos.extend(json.loads(s["Document"]) for s in traza.get("Segments", []))
                if not respuesta.get("NextToken"):
                    break
                parametros["NextToken"] = respuesta["NextToken"]
        return documentos


def crear_repositorio(region: str) -> RepositorioXRay:
    import boto3

    return RepositorioXRay(boto3.client("xray", region_name=region))
