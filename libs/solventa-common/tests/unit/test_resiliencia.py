import asyncio

import httpx
import pytest
from solventa_common.resiliencia import (
    CORTE_DURO_MS,
    DependenciaNoDisponible,
    SinProteccion,
    TiempoAgotado,
    timeout_efectivo_ms,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.parametrize(
    ("configurado", "deadline", "esperado"),
    [
        (300, None, 300),  # manda el configurado
        (1000, None, CORTE_DURO_MS),  # manda el corte duro
        (700, 290, 270),  # manda el deadline menos el margen
        (700, 10, 1),  # nunca menor que 1
        (0, None, 1),
    ],
)
async def test_timeout_efectivo(configurado: int, deadline: int | None, esperado: int) -> None:
    assert timeout_efectivo_ms(configurado, deadline) == esperado


async def test_timeout_efectivo_con_margen_propio() -> None:
    assert timeout_efectivo_ms(700, 300, margen_ms=50) == 250


async def test_exito_pasa_el_timeout_en_segundos() -> None:
    recibidos: list[float] = []

    async def llamada(timeout_s: float) -> str:
        recibidos.append(timeout_s)
        return "ok"

    assert await SinProteccion().ejecutar("F", "G", llamada, 300, 290) == "ok"
    assert recibidos == [0.27]


@pytest.mark.parametrize(
    "error",
    [httpx.ReadTimeout("lento"), TimeoutError()],
)
async def test_timeout_se_traduce(error: Exception) -> None:
    async def llamada(_: float) -> None:
        raise error

    with pytest.raises(TiempoAgotado):
        await SinProteccion().ejecutar("F", "G", llamada, 300, None)


async def test_llamada_que_no_respeta_su_timeout_se_corta() -> None:
    async def llamada(_: float) -> None:
        await asyncio.sleep(1)

    with pytest.raises(TiempoAgotado):
        await SinProteccion().ejecutar("F", "G", llamada, 700, 40)


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("sin conexión"),
        httpx.HTTPStatusError(
            "503",
            request=httpx.Request("GET", "http://aliado"),
            response=httpx.Response(503),
        ),
    ],
)
async def test_error_se_traduce_a_dependencia_no_disponible(error: Exception) -> None:
    async def llamada(_: float) -> None:
        raise error

    with pytest.raises(DependenciaNoDisponible) as capturado:
        await SinProteccion().ejecutar("F", "G", llamada, 300, None)
    assert (capturado.value.motivo, capturado.value.circuito) == ("ERROR", "CERRADO")


async def test_otras_excepciones_no_se_traducen() -> None:
    async def llamada(_: float) -> None:
        raise ValueError("respuesta inválida")

    with pytest.raises(ValueError):
        await SinProteccion().ejecutar("F", "G", llamada, 300, None)
