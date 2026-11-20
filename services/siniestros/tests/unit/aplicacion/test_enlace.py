"""URL prefirmada de una evidencia (DS3-08)."""

from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from siniestros.application.siniestro.enlace import TIPO_CONTENIDO_GENERICO, EmitirEnlace
from siniestros.application.siniestro.errores import (
    DependenciaNoDisponible,
    EvidenciaNoDisponible,
    EvidenciaNoEncontrada,
    SiniestroNoEncontrado,
)
from siniestros.application.siniestro.vistas import Actor
from siniestros.domain.siniestro.modelos import Evidencia, IntegridadEvidencia, Siniestro

pytestmark = pytest.mark.anyio
ANALISTA = UUID("a0a0a0a0-0000-4000-8000-000000000001")
ACTOR = Actor(str(ANALISTA), "ANALISTA_SINIESTROS", "Luis Mora")


@pytest.fixture
def escenario(repo, siniestro: Callable[..., Siniestro], evidencia: Callable[..., Evidencia]):  # type: ignore[no-untyped-def]
    caso = siniestro()
    archivo = evidencia(siniestro_id=caso.id, tipo_contenido="image/jpeg")
    repo.agregar(caso, archivo)
    return caso, archivo


@pytest.fixture
def emitir(repo, almacen, reloj):  # type: ignore[no-untyped-def]
    return EmitirEnlace(repo, almacen, reloj, ttl_s=300)


async def test_emite_la_url_con_su_vigencia_y_su_tipo(
    emitir: EmitirEnlace, almacen, escenario, ahora: datetime
) -> None:
    caso, archivo = escenario
    enlace = await emitir.ejecutar(caso.id, archivo.id, ACTOR)
    assert enlace.url == almacen.enlace
    assert enlace.expira_en == ahora + timedelta(seconds=300)
    assert enlace.tipo_contenido == "image/jpeg"
    assert almacen.firmadas == [(archivo.clave_objeto, 300, "image/jpeg")]


async def test_sin_tipo_registrado_usa_el_generico(
    emitir: EmitirEnlace,
    repo,
    siniestro: Callable[..., Siniestro],
    evidencia: Callable[..., Evidencia],
) -> None:
    caso = siniestro()
    archivo = evidencia(siniestro_id=caso.id, tipo_contenido=None)
    repo.agregar(caso, archivo)
    enlace = await emitir.ejecutar(caso.id, archivo.id, ACTOR)
    assert enlace.tipo_contenido == TIPO_CONTENIDO_GENERICO


async def test_el_evento_no_lleva_la_url_ni_la_clave(
    emitir: EmitirEnlace, repo, almacen, escenario, ahora: datetime
) -> None:
    caso, archivo = escenario
    await emitir.ejecutar(caso.id, archivo.id, ACTOR)
    (evento,) = repo.eventos
    assert evento == (
        "EvidenciaConsultada",
        archivo.id,
        str(ANALISTA),
        ahora + timedelta(seconds=300),
    )
    texto = repr(evento)
    assert almacen.enlace not in texto
    assert archivo.clave_objeto not in texto


@pytest.mark.parametrize("integridad", list(IntegridadEvidencia)[:2])
async def test_un_objeto_alterado_o_verificado_tambien_se_firma(
    emitir: EmitirEnlace, almacen, escenario, integridad: IntegridadEvidencia
) -> None:
    caso, archivo = escenario
    almacen.integridad[archivo.clave_objeto] = integridad
    assert (await emitir.ejecutar(caso.id, archivo.id, ACTOR)).url == almacen.enlace


async def test_un_objeto_ausente_es_404_sin_url_ni_evento(
    emitir: EmitirEnlace, repo, almacen, escenario
) -> None:
    caso, archivo = escenario
    almacen.integridad[archivo.clave_objeto] = IntegridadEvidencia.NO_DISPONIBLE
    with pytest.raises(EvidenciaNoDisponible) as error:
        await emitir.ejecutar(caso.id, archivo.id, ACTOR)
    assert (error.value.codigo, error.value.estado_http) == ("EVIDENCIA_NO_DISPONIBLE", 404)
    assert almacen.firmadas == []
    assert repo.eventos == []


async def test_si_no_se_puede_confirmar_el_objeto_es_503_sin_firmar(
    emitir: EmitirEnlace, repo, almacen, escenario
) -> None:
    caso, archivo = escenario
    almacen.integridad[archivo.clave_objeto] = IntegridadEvidencia.NO_VERIFICABLE
    with pytest.raises(DependenciaNoDisponible) as error:
        await emitir.ejecutar(caso.id, archivo.id, ACTOR)
    assert (error.value.codigo, error.value.estado_http) == ("DEPENDENCIA_NO_DISPONIBLE", 503)
    assert error.value.detalles == {"dependencia": "evidencias"}
    assert almacen.firmadas == []
    assert repo.eventos == []


async def test_si_falla_la_firma_no_queda_evento(
    emitir: EmitirEnlace, repo, almacen, escenario
) -> None:
    caso, archivo = escenario
    almacen.falla_al_firmar = True
    with pytest.raises(DependenciaNoDisponible):
        await emitir.ejecutar(caso.id, archivo.id, ACTOR)
    assert repo.eventos == []


async def test_siniestro_inexistente_y_evidencia_inexistente_o_ajena(
    emitir: EmitirEnlace,
    repo,
    escenario,
    siniestro: Callable[..., Siniestro],
    evidencia: Callable[..., Evidencia],
) -> None:
    caso, archivo = escenario
    with pytest.raises(SiniestroNoEncontrado):
        await emitir.ejecutar(uuid4(), archivo.id, ACTOR)
    with pytest.raises(EvidenciaNoEncontrada) as error:
        await emitir.ejecutar(caso.id, uuid4(), ACTOR)
    assert (error.value.codigo, error.value.estado_http) == ("EVIDENCIA_NO_ENCONTRADA", 404)
    # Una evidencia de otro siniestro no se puede pedir por este.
    otro = siniestro()
    ajena = evidencia(siniestro_id=otro.id)
    repo.agregar(otro, ajena)
    with pytest.raises(EvidenciaNoEncontrada):
        await emitir.ejecutar(caso.id, ajena.id, ACTOR)
    assert repo.eventos == []
