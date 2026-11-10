"""Tomar el siguiente de la cola (DS3-03)."""

from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from siniestros.application.siniestro.errores import ColaSinCasos, IdempotenciaConflicto
from siniestros.application.siniestro.tomar import TomarSiguiente, huella_de
from siniestros.application.siniestro.vistas import Actor, Plazos
from siniestros.domain.siniestro.modelos import EstadoSiniestro, PrioridadSiniestro, Siniestro

pytestmark = pytest.mark.anyio
ANALISTA = UUID("a0a0a0a0-0000-4000-8000-000000000001")
OTRO = UUID("a0a0a0a0-0000-4000-8000-000000000002")
ACTOR = Actor(str(ANALISTA), "ANALISTA_SINIESTROS", "Luis Mora")


@pytest.fixture
def caso(repo, reloj, plazos):  # type: ignore[no-untyped-def]
    return TomarSiguiente(repo, reloj, plazos)


async def test_asigna_el_caso_al_actor_y_lo_pasa_a_evaluacion(
    caso: TomarSiguiente, repo, siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    libre = siniestro()
    repo.agregar(libre)
    resumen = await caso.ejecutar(ACTOR, ANALISTA, uuid4())
    guardado = repo.siniestros[libre.id]
    assert guardado.estado is EstadoSiniestro.EN_EVALUACION
    assert guardado.analista_id == ANALISTA
    assert guardado.asignado_en == ahora
    assert resumen.siniestro == guardado
    assert resumen.tiempo_restante_min == 2040
    assert resumen.prioridad is PrioridadSiniestro.BAJA


async def test_deja_el_historial_el_evento_y_la_idempotencia_en_la_misma_transaccion(
    caso: TomarSiguiente, repo, siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    libre = siniestro()
    repo.agregar(libre)
    clave = uuid4()
    await caso.ejecutar(ACTOR, ANALISTA, clave)
    (entrada,) = repo.historial
    assert (entrada.accion, entrada.estado_anterior, entrada.estado_nuevo) == (
        "ASIGNADO",
        EstadoSiniestro.REPORTADO,
        EstadoSiniestro.EN_EVALUACION,
    )
    assert (entrada.actor_id, entrada.actor_rol, entrada.actor_nombre) == (
        str(ANALISTA),
        "ANALISTA_SINIESTROS",
        "Luis Mora",
    )
    assert entrada.ocurrido_en == ahora
    assert repo.eventos == [("SiniestroEnEvaluacion", libre.id, ahora, "ASIGNACION")]
    assert repo.idempotencia[f"siguiente:{clave}"].respuesta == {"siniestroId": str(libre.id)}
    assert repo.transacciones == 1


async def test_toma_el_mas_antiguo(
    caso: TomarSiguiente, repo, siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    nuevo = siniestro(
        reportado_en=ahora - timedelta(hours=1), fecha_evento=ahora - timedelta(hours=2)
    )
    viejo = siniestro(
        reportado_en=ahora - timedelta(hours=30), fecha_evento=ahora - timedelta(hours=31)
    )
    repo.agregar(nuevo)
    repo.agregar(viejo)
    resumen = await caso.ejecutar(ACTOR, ANALISTA, uuid4())
    assert resumen.siniestro.id == viejo.id
    assert repo.siniestros[nuevo.id].estado is EstadoSiniestro.REPORTADO


async def test_no_toma_un_caso_ya_asignado(
    caso: TomarSiguiente, repo, siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    repo.agregar(
        siniestro(estado=EstadoSiniestro.EN_EVALUACION, analista_id=OTRO, asignado_en=ahora)
    )
    with pytest.raises(ColaSinCasos) as error:
        await caso.ejecutar(ACTOR, ANALISTA, uuid4())
    assert (error.value.codigo, error.value.estado_http) == ("COLA_SIN_CASOS", 404)


async def test_cola_vacia_no_escribe_nada(caso: TomarSiguiente, repo) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ColaSinCasos):
        await caso.ejecutar(ACTOR, ANALISTA, uuid4())
    assert repo.historial == []
    assert repo.eventos == []
    assert repo.idempotencia == {}


async def test_la_misma_clave_devuelve_el_mismo_caso_sin_repetir_nada(
    caso: TomarSiguiente, repo, siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    primero, segundo = siniestro(), siniestro(reportado_en=ahora - timedelta(hours=1))
    repo.agregar(primero)
    repo.agregar(segundo)
    clave = uuid4()
    uno = await caso.ejecutar(ACTOR, ANALISTA, clave)
    otra_vez = await caso.ejecutar(ACTOR, ANALISTA, clave)
    assert otra_vez.siniestro.id == uno.siniestro.id == primero.id
    assert len(repo.historial) == 1
    assert len(repo.eventos) == 1
    # El segundo caso sigue libre.
    assert repo.siniestros[segundo.id].estado is EstadoSiniestro.REPORTADO


async def test_la_misma_clave_de_otro_analista_es_un_conflicto(
    caso: TomarSiguiente, repo, siniestro: Callable[..., Siniestro]
) -> None:
    repo.agregar(siniestro())
    clave = uuid4()
    await caso.ejecutar(ACTOR, ANALISTA, clave)
    with pytest.raises(IdempotenciaConflicto) as error:
        await caso.ejecutar(Actor(str(OTRO), "ANALISTA_SINIESTROS", "Ana"), OTRO, clave)
    assert (error.value.codigo, error.value.estado_http) == ("IDEMPOTENCIA_CONFLICTO", 422)


async def test_el_resumen_usa_los_plazos_configurados(
    repo, reloj, siniestro: Callable[..., Siniestro]
) -> None:
    repo.agregar(siniestro())
    corto = TomarSiguiente(repo, reloj, Plazos(compromiso_horas=15, alta_min=480, media_min=1440))
    resumen = await corto.ejecutar(ACTOR, ANALISTA, uuid4())
    # 15 h de compromiso, 14 h corridas: queda 1 h.
    assert resumen.tiempo_restante_min == 60
    assert resumen.prioridad is PrioridadSiniestro.ALTA


def test_la_huella_depende_del_analista() -> None:
    assert huella_de(ANALISTA) == huella_de(ANALISTA)
    assert huella_de(ANALISTA) != huella_de(OTRO)
