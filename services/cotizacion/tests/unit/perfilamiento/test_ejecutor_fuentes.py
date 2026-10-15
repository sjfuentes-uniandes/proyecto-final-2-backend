"""EjecutorSecuencial provisional (UT-W11-28 y 29)."""

import asyncio
import time
from typing import Any
from uuid import UUID

import pytest
from cotizacion.application.perfilamiento.ejecutor_fuentes import EjecutorSecuencial
from cotizacion.domain.perfilamiento.modelos import (
    ContextoConsulta,
    EstadoFuente,
    Fuente,
    ResultadoFuente,
    SolicitudFuente,
)

pytestmark = pytest.mark.anyio

CONTEXTO = ContextoConsulta(UUID(int=1), UUID(int=2), "PERFILAMIENTO_PRECIO", 290)


class PuertoQueRegistra:
    def __init__(self) -> None:
        self.intervalos: list[tuple[Fuente, float, float]] = []
        self.contextos: list[ContextoConsulta] = []

    async def consultar(self, solicitud: SolicitudFuente, contexto: ContextoConsulta) -> Any:
        inicio = time.perf_counter()
        await asyncio.sleep(0.01)
        self.intervalos.append((solicitud.fuente, inicio, time.perf_counter()))
        self.contextos.append(contexto)
        return ResultadoFuente(
            solicitud.fuente,
            EstadoFuente.CONSULTADA,
            solicitud.campos,
            {},
            None,
            None,
            None,
            10,
            None,
        )


async def test_ut_w11_28_consulta_una_tras_otra_y_en_orden() -> None:
    puerto = PuertoQueRegistra()
    solicitudes = [SolicitudFuente(f, ("x",)) for f in reversed(list(Fuente))]
    resultados = await EjecutorSecuencial(puerto).ejecutar(solicitudes, CONTEXTO)
    assert [r.fuente for r in resultados] == [s.fuente for s in solicitudes]
    assert [f for f, _, _ in puerto.intervalos] == [s.fuente for s in solicitudes]
    for (_, _, fin), (_, inicio, _) in zip(puerto.intervalos, puerto.intervalos[1:], strict=False):
        assert fin <= inicio
    assert puerto.contextos == [CONTEXTO] * 4


async def test_ut_w11_29_sin_solicitudes_no_llama_al_puerto() -> None:
    puerto = PuertoQueRegistra()
    assert await EjecutorSecuencial(puerto).ejecutar([], CONTEXTO) == []
    assert puerto.intervalos == []
