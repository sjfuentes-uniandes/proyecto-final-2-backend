"""Reconstrucción del resultado (UT-W11-25 a 27)."""

from dataclasses import replace
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from cotizacion.application.perfilamiento.reconstruir_perfil import ReconstruirPerfil
from cotizacion.domain.perfilamiento.errores import PerfilNoEncontrado

pytestmark = pytest.mark.anyio


@pytest.fixture
def reconstruir(escenario: Any, perfil_c01: Any) -> ReconstruirPerfil:
    escenario.repo.perfiles[perfil_c01.id] = perfil_c01
    return ReconstruirPerfil(escenario.repo, escenario.modelos)


async def test_ut_w11_25_c01_coincide(
    reconstruir: ReconstruirPerfil, perfil_c01: Any, escenario: Any
) -> None:
    assert await reconstruir.ejecutar(perfil_c01.id) == (True, "2026.10.1", perfil_c01.hash_modelo)
    assert escenario.modelos.llamadas == [("2026.10.1",)]


async def test_ut_w11_26_insumos_alterados_no_coinciden(
    reconstruir: ReconstruirPerfil, perfil_c01: Any, escenario: Any
) -> None:
    alterado = replace(perfil_c01, insumos={**perfil_c01.insumos, "moras_12m": Decimal(1)})
    escenario.repo.perfiles[perfil_c01.id] = alterado
    coincide, _, _ = await reconstruir.ejecutar(perfil_c01.id)
    assert coincide is False


async def test_ut_w11_27_hash_distinto_no_coincide(
    reconstruir: ReconstruirPerfil, perfil_c01: Any, escenario: Any
) -> None:
    escenario.repo.perfiles[perfil_c01.id] = replace(perfil_c01, hash_modelo="sha256:otro")
    assert await reconstruir.ejecutar(perfil_c01.id) == (False, "2026.10.1", "sha256:otro")


async def test_perfil_inexistente(reconstruir: ReconstruirPerfil) -> None:
    with pytest.raises(PerfilNoEncontrado):
        await reconstruir.ejecutar(UUID(int=404))
