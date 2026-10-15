from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from catalogo.application.reglas.consultar_modelo import ConsultarModelo
from catalogo.application.reglas.publicar_modelo import PublicarModelo
from catalogo.domain.reglas.errores import (
    DefinicionInvalida,
    ModeloNoEncontrado,
    VersionExistente,
    VigenciaRetroactiva,
)
from catalogo.domain.reglas.hash import hash_canonico
from catalogo.domain.reglas.modelo import ModeloPublicado

pytestmark = pytest.mark.anyio


async def _publicar(
    repositorio: Any,
    reloj: Any,
    definicion: dict[str, Any],
    version: str = "2026.10.2",
    vigente_desde: datetime | None = None,
) -> ModeloPublicado:
    return await PublicarModelo(repositorio, reloj).ejecutar(
        version=version,
        producto_id="vida-hipotecario",
        vigente_desde=vigente_desde or reloj.ahora(),
        definicion=definicion,
        actor="actuaria-sintetica",
    )


async def test_publicar_un_modelo_valido(
    repositorio: Any, reloj: Any, definicion_candidata: dict[str, Any]
) -> None:
    definicion_candidata["niveles"][0]["desde"] = "80"
    modelo = await _publicar(repositorio, reloj, definicion_candidata)
    assert modelo.hash == hash_canonico(definicion_candidata)
    assert modelo.publicado_por == "actuaria-sintetica"
    assert repositorio.modelos["2026.10.2"] == modelo


async def test_publicar_una_definicion_invalida(
    repositorio: Any, reloj: Any, definicion_candidata: dict[str, Any]
) -> None:
    definicion_candidata["factores"][0]["peso"] = "0.31"
    with pytest.raises(DefinicionInvalida) as error:
        await _publicar(repositorio, reloj, definicion_candidata)
    assert error.value.errores == ("PESOS_NO_SUMAN_1",)
    assert "2026.10.2" not in repositorio.modelos


@pytest.mark.parametrize(
    ("atraso", "aceptada"),
    [
        (timedelta(seconds=5), True),
        (timedelta(seconds=5, milliseconds=1), False),
        (timedelta(0), True),
        (-timedelta(days=30), True),
    ],
    ids=["limite-5s", "5.001s", "ahora", "futuro"],
)
async def test_limite_de_vigencia_retroactiva(
    repositorio: Any,
    reloj: Any,
    definicion_candidata: dict[str, Any],
    atraso: timedelta,
    aceptada: bool,
) -> None:
    vigente_desde = reloj.ahora() - atraso
    if aceptada:
        modelo = await _publicar(
            repositorio, reloj, definicion_candidata, vigente_desde=vigente_desde
        )
        assert modelo.vigente_desde == vigente_desde
    else:
        with pytest.raises(VigenciaRetroactiva):
            await _publicar(repositorio, reloj, definicion_candidata, vigente_desde=vigente_desde)


async def test_version_existente(
    repositorio: Any, reloj: Any, definicion_candidata: dict[str, Any]
) -> None:
    with pytest.raises(VersionExistente):
        await _publicar(repositorio, reloj, definicion_candidata, version="2026.10.1")


async def test_consultar_vigente_y_por_version(
    repositorio: Any, reloj: Any, definicion_candidata: dict[str, Any], modelo_candidato: Any
) -> None:
    nueva = await _publicar(repositorio, reloj, definicion_candidata)
    consultar = ConsultarModelo(repositorio)
    assert await consultar.vigente("vida-hipotecario", reloj.ahora()) == nueva
    antes = reloj.ahora() - timedelta(microseconds=1)
    assert await consultar.vigente("vida-hipotecario", antes) == modelo_candidato
    assert await consultar.por_version("2026.10.1") == modelo_candidato


async def test_no_encontrado(repositorio: Any) -> None:
    consultar = ConsultarModelo(repositorio)
    with pytest.raises(ModeloNoEncontrado):
        await consultar.por_version("2030.01.1")
    with pytest.raises(ModeloNoEncontrado):
        await consultar.vigente("otro-producto", datetime(2026, 10, 15, tzinfo=UTC))
    with pytest.raises(ModeloNoEncontrado):
        await consultar.vigente("vida-hipotecario", datetime(2026, 9, 30, tzinfo=UTC))
