"""ConsultarReglasSuscripcion (HU-W13): la fecha por omisión es el reloj y, sin reglas, distingue
el producto inexistente de las reglas que faltan."""

from datetime import UTC, datetime
from typing import Any

import pytest
from catalogo.application.suscripcion.consultar_reglas import ConsultarReglasSuscripcion
from catalogo.domain.productos.errores import ProductoNoEncontrado
from catalogo.domain.suscripcion.errores import ReglasSuscripcionNoVigentes

pytestmark = pytest.mark.anyio


class Espia:
    """Repositorio de reglas que registra la fecha con la que se consultó."""

    def __init__(self, repo: Any) -> None:
        self.repo = repo
        self.fechas: list[datetime] = []

    async def vigentes(self, producto_id: str, en: datetime) -> Any:
        self.fechas.append(en)
        return await self.repo.vigentes(producto_id, en)

    async def por_version(self, producto_id: str, version: str) -> Any:
        return await self.repo.por_version(producto_id, version)


async def test_sin_fecha_usa_el_reloj(
    reglas_suscripcion_repo: Any, productos_repo: Any, reloj: Any
) -> None:
    espia = Espia(reglas_suscripcion_repo)
    caso = ConsultarReglasSuscripcion(espia, productos_repo, reloj)
    reglas = await caso.vigentes("vida-hipotecario", None)
    assert reglas.version == "2026.10.1"
    fecha = datetime(2026, 10, 2, tzinfo=UTC)
    await caso.vigentes("vida-hipotecario", fecha)
    assert espia.fechas == [reloj.ahora(), fecha]


async def test_distingue_producto_inexistente_de_reglas_faltantes(
    reglas_suscripcion_repo: Any, productos_repo: Any, reloj: Any
) -> None:
    caso = ConsultarReglasSuscripcion(reglas_suscripcion_repo, productos_repo, reloj)
    with pytest.raises(ProductoNoEncontrado):
        await caso.vigentes("no-existe", None)
    with pytest.raises(ReglasSuscripcionNoVigentes):
        await caso.vigentes("vida-hipotecario-2025", None)
    with pytest.raises(ReglasSuscripcionNoVigentes):
        await caso.por_version("vida-hipotecario", "2030.01.1")
    assert (await caso.por_version("vida-hipotecario", "2026.10.1")).publicado_por == "migracion"
