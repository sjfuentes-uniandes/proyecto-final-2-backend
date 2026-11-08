"""Detalle del siniestro: cobertura, coherencia, trámite y auditoría (AC1, AC4)."""

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from siniestros.application.siniestro.detalle import ConsultarDetalle
from siniestros.application.siniestro.errores import SiniestroNoEncontrado
from siniestros.application.siniestro.vistas import Actor, Plazos
from siniestros.domain.siniestro.modelos import (
    CodigoVerificacion,
    EntradaHistorial,
    EstadoPaso,
    EstadoSiniestro,
    Evidencia,
    IntegridadEvidencia,
    ResultadoCoherencia,
    ResultadoVerificacion,
    Siniestro,
    TipoSoporte,
)

pytestmark = pytest.mark.anyio
ANALISTA = UUID("a0a0a0a0-0000-4000-8000-000000000001")
ACTOR = Actor(str(ANALISTA), "ANALISTA_SINIESTROS", "Luis Mora")


@pytest.fixture
def caso_h03(
    repo,  # type: ignore[no-untyped-def]
    siniestro: Callable[..., Siniestro],
    evidencia: Callable[..., Evidencia],
    soporte: Callable[[TipoSoporte], Evidencia],
    entrada: Callable[..., EntradaHistorial],
    ahora: datetime,
):
    """EVA-01: cinco archivos y falta el certificado de reparación."""
    reportado = ahora - timedelta(hours=14)
    caso = siniestro(
        estado=EstadoSiniestro.EN_EVALUACION,
        analista_id=ANALISTA,
        asignado_en=ahora - timedelta(hours=12),
    )
    archivos = [
        evidencia(siniestro_id=caso.id, capturada_en=reportado - timedelta(minutes=7)),
        evidencia(siniestro_id=caso.id, capturada_en=reportado - timedelta(minutes=6)),
        evidencia(siniestro_id=caso.id, capturada_en=reportado - timedelta(minutes=5)),
    ]
    for tipo in (TipoSoporte.FACTURA_COMPRA, TipoSoporte.DOCUMENTO_IDENTIDAD):
        archivo = soporte(tipo)
        archivos.append(
            evidencia(
                id=archivo.id,
                siniestro_id=caso.id,
                nombre_archivo=archivo.nombre_archivo,
                tipo=archivo.tipo,
                soporte_tipo=tipo,
                capturada_en=reportado - timedelta(minutes=4),
                ubicacion=None,
            )
        )
    repo.agregar(caso, *archivos)
    repo.historial.extend(
        [
            entrada(
                "REPORTADO", reportado, siniestro_id=caso.id, estado_nuevo=EstadoSiniestro.REPORTADO
            ),
            entrada(
                "EVIDENCIAS_VALIDADAS", reportado + timedelta(minutes=16), siniestro_id=caso.id
            ),
            entrada(
                "ASIGNADO",
                reportado + timedelta(minutes=120),
                siniestro_id=caso.id,
                estado_anterior=EstadoSiniestro.REPORTADO,
                estado_nuevo=EstadoSiniestro.EN_EVALUACION,
                actor_nombre="Luis Mora",
            ),
        ]
    )
    return caso, archivos


@pytest.fixture
def consultar(repo, almacen, soportes, reloj, plazos: Plazos):  # type: ignore[no-untyped-def]
    return ConsultarDetalle(repo, almacen, soportes, reloj, plazos)


async def test_arma_el_detalle_del_caso_h03(consultar: ConsultarDetalle, caso_h03) -> None:  # type: ignore[no-untyped-def]
    caso, _ = caso_h03
    detalle = await consultar.ejecutar(caso.id, ACTOR)
    assert detalle.resumen.siniestro == caso
    assert detalle.resumen.tiempo_restante_min == 2040
    assert {v.codigo: v.resultado for v in detalle.verificacion_cobertura} == {
        CodigoVerificacion.VIGENCIA: ResultadoVerificacion.CUMPLE,
        CodigoVerificacion.AMPARO: ResultadoVerificacion.CUMPLE,
        CodigoVerificacion.CARENCIA: ResultadoVerificacion.CUMPLE,
        CodigoVerificacion.PLAZO_AVISO: ResultadoVerificacion.CUMPLE,
        CodigoVerificacion.PRIMAS: ResultadoVerificacion.CUMPLE,
        CodigoVerificacion.SOPORTES: ResultadoVerificacion.PARCIAL,
    }
    assert detalle.coherencia.resultado is ResultadoCoherencia.COHERENTE
    assert len(detalle.evidencias) == 5
    assert all(e.integridad is IntegridadEvidencia.VERIFICADA for e in detalle.evidencias)
    assert [p.tipo for p in detalle.soportes_pendientes] == [TipoSoporte.CERTIFICADO_REPARACION]
    assert [p.estado for p in detalle.tramite] == [
        EstadoPaso.COMPLETADO,
        EstadoPaso.COMPLETADO,
        EstadoPaso.ACTUAL,
        EstadoPaso.PENDIENTE,
        EstadoPaso.PENDIENTE,
    ]
    assert detalle.tramite[2].actor_nombre == "Luis Mora"
    assert len(detalle.historial) == 3


async def test_escribe_siniestro_consultado_y_no_asigna_el_caso(
    consultar: ConsultarDetalle, repo, caso_h03
) -> None:
    caso, _ = caso_h03
    await consultar.ejecutar(caso.id, ACTOR)
    assert repo.eventos == [("SiniestroConsultado", caso.id, str(ANALISTA))]
    assert repo.siniestros[caso.id] == caso


async def test_abrir_un_caso_sin_asignar_no_lo_asigna(
    consultar: ConsultarDetalle, repo, siniestro: Callable[..., Siniestro]
) -> None:
    libre = siniestro()
    repo.agregar(libre)
    detalle = await consultar.ejecutar(libre.id, ACTOR)
    assert detalle.resumen.siniestro.analista_id is None
    assert repo.siniestros[libre.id].estado is EstadoSiniestro.REPORTADO
    assert repo.historial == []


async def test_un_objeto_ausente_no_impide_la_respuesta(
    consultar: ConsultarDetalle, almacen, caso_h03
) -> None:
    caso, archivos = caso_h03
    almacen.integridad[archivos[2].clave_objeto] = IntegridadEvidencia.NO_DISPONIBLE
    detalle = await consultar.ejecutar(caso.id, ACTOR)
    assert [e.integridad for e in detalle.evidencias].count(IntegridadEvidencia.NO_DISPONIBLE) == 1
    # No se sabe que esté alterado: la coherencia sigue.
    assert detalle.coherencia.resultado is ResultadoCoherencia.COHERENTE


async def test_un_archivo_alterado_vuelve_inconsistente_la_coherencia(
    consultar: ConsultarDetalle, almacen, caso_h03
) -> None:
    caso, archivos = caso_h03
    almacen.integridad[archivos[0].clave_objeto] = IntegridadEvidencia.ALTERADA
    detalle = await consultar.ejecutar(caso.id, ACTOR)
    assert detalle.coherencia.archivos_alterados == 1
    assert detalle.coherencia.resultado is ResultadoCoherencia.INCONSISTENTE


async def test_verifica_los_objetos_en_paralelo_y_sin_transaccion_abierta(
    consultar: ConsultarDetalle, almacen, repo, caso_h03
) -> None:
    caso, archivos = caso_h03
    almacen.espera = asyncio.Event()

    async def liberar() -> None:
        while almacen.simultaneas < len(archivos):
            await asyncio.sleep(0)
        almacen.espera.set()  # type: ignore[union-attr]

    liberador = asyncio.create_task(liberar())
    await asyncio.wait_for(consultar.ejecutar(caso.id, ACTOR), timeout=5)
    await liberador
    assert almacen.max_simultaneas == len(archivos)
    assert sorted(almacen.verificadas) == sorted(a.clave_objeto for a in archivos)
    # El almacén afirma que ninguna llamada corrió con una transacción abierta; solo hubo una, la del
    # evento de auditoría, después de verificar.
    assert repo.transacciones == 1


async def test_un_caso_sin_evidencias_no_llama_al_almacen(
    consultar: ConsultarDetalle, almacen, repo, siniestro: Callable[..., Siniestro]
) -> None:
    libre = siniestro()
    repo.agregar(libre)
    detalle = await consultar.ejecutar(libre.id, ACTOR)
    assert detalle.evidencias == ()
    assert almacen.verificadas == []
    assert detalle.coherencia.resultado is ResultadoCoherencia.NO_EVALUABLE


async def test_un_siniestro_inexistente_es_404_y_no_deja_evento(
    consultar: ConsultarDetalle, repo, almacen
) -> None:
    with pytest.raises(SiniestroNoEncontrado) as error:
        await consultar.ejecutar(uuid4(), ACTOR)
    assert (error.value.codigo, error.value.estado_http) == ("SINIESTRO_NO_ENCONTRADO", 404)
    assert repo.eventos == []
    assert almacen.verificadas == []


async def test_el_amparo_sin_soportes_configurados_no_es_evaluable(
    repo, almacen, reloj, plazos: Plazos, caso_h03, soportes
) -> None:
    caso, _ = caso_h03
    soportes.requeridos = {}
    detalle = await ConsultarDetalle(repo, almacen, soportes, reloj, plazos).ejecutar(
        caso.id, ACTOR
    )
    por_codigo = {v.codigo: v.resultado for v in detalle.verificacion_cobertura}
    assert por_codigo[CodigoVerificacion.SOPORTES] is ResultadoVerificacion.NO_EVALUABLE
    assert detalle.soportes_pendientes == ()
