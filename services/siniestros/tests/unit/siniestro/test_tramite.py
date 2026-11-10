"""Línea de tiempo del trámite (H03), derivada del estado y del historial."""

from collections.abc import Callable
from datetime import datetime, timedelta

from siniestros.domain.siniestro.modelos import (
    EntradaHistorial,
    EstadoPaso,
    EstadoSiniestro,
    PasoTramite,
)
from siniestros.domain.siniestro.tramite import PasoDeTramite, pasos_tramite

ESTADO = {EstadoPaso.COMPLETADO: "C", EstadoPaso.ACTUAL: "A", EstadoPaso.PENDIENTE: "P"}


def resumen(pasos: tuple[PasoDeTramite, ...]) -> str:
    return "".join(ESTADO[p.estado] for p in pasos)


def historial_eva01(
    entrada: Callable[..., EntradaHistorial], ahora: datetime
) -> list[EntradaHistorial]:
    reportado = ahora - timedelta(hours=14)
    return [
        entrada(
            "REPORTADO", reportado, estado_nuevo=EstadoSiniestro.REPORTADO, actor_rol="CLIENTE"
        ),
        entrada("EVIDENCIAS_VALIDADAS", reportado + timedelta(minutes=16), actor_rol="SISTEMA"),
        entrada(
            "ASIGNADO",
            reportado + timedelta(minutes=120),
            estado_anterior=EstadoSiniestro.REPORTADO,
            estado_nuevo=EstadoSiniestro.EN_EVALUACION,
            actor_rol="ANALISTA_SINIESTROS",
            actor_nombre="Luis Mora",
        ),
    ]


def test_los_cinco_pasos_siempre_en_orden(entrada: Callable[..., EntradaHistorial]) -> None:
    pasos = pasos_tramite(EstadoSiniestro.REPORTADO, [])
    assert [p.paso for p in pasos] == list(PasoTramite)


def test_un_caso_en_evaluacion_muestra_al_analista_en_el_paso_actual(
    entrada: Callable[..., EntradaHistorial], ahora: datetime
) -> None:
    historial = historial_eva01(entrada, ahora)
    pasos = pasos_tramite(EstadoSiniestro.EN_EVALUACION, historial)
    assert resumen(pasos) == "CCAPP"
    actual = pasos[2]
    assert actual.paso is PasoTramite.EN_EVALUACION
    assert actual.actor_nombre == "Luis Mora"
    assert actual.ocurrido_en == historial[2].ocurrido_en
    assert pasos[0].ocurrido_en == historial[0].ocurrido_en
    assert pasos[3].ocurrido_en is None
    assert pasos[4].actor_nombre is None


def test_un_reportado_sin_historial_espera_las_evidencias() -> None:
    pasos = pasos_tramite(EstadoSiniestro.REPORTADO, [])
    assert resumen(pasos) == "APPPP"


def test_un_reportado_con_evidencias_validadas_espera_a_un_analista(
    entrada: Callable[..., EntradaHistorial], ahora: datetime
) -> None:
    historial = historial_eva01(entrada, ahora)[:2]
    pasos = pasos_tramite(EstadoSiniestro.REPORTADO, historial)
    assert resumen(pasos) == "CCAPP"
    assert pasos[2].ocurrido_en is None


def test_falta_soporte_deja_el_caso_en_evaluacion(
    entrada: Callable[..., EntradaHistorial], ahora: datetime
) -> None:
    pasos = pasos_tramite(EstadoSiniestro.FALTA_SOPORTE, historial_eva01(entrada, ahora))
    assert resumen(pasos) == "CCAPP"


def test_un_caso_decidido_completa_la_decision(
    entrada: Callable[..., EntradaHistorial], ahora: datetime
) -> None:
    historial = historial_eva01(entrada, ahora) + [
        entrada(
            "APROBADO",
            ahora - timedelta(hours=1),
            estado_anterior=EstadoSiniestro.EN_EVALUACION,
            estado_nuevo=EstadoSiniestro.APROBADO,
            actor_nombre="Luis Mora",
        )
    ]
    pasos = pasos_tramite(EstadoSiniestro.APROBADO, historial)
    assert resumen(pasos) == "CCCCA"
    assert pasos[3].ocurrido_en == ahora - timedelta(hours=1)
    assert pasos[4].ocurrido_en is None


def test_un_rechazo_completa_la_decision_y_no_deja_paso_actual(
    entrada: Callable[..., EntradaHistorial], ahora: datetime
) -> None:
    historial = historial_eva01(entrada, ahora) + [
        entrada(
            "RECHAZADO",
            ahora - timedelta(hours=1),
            estado_anterior=EstadoSiniestro.EN_EVALUACION,
            estado_nuevo=EstadoSiniestro.RECHAZADO,
            actor_nombre="Luis Mora",
        )
    ]
    pasos = pasos_tramite(EstadoSiniestro.RECHAZADO, historial)
    # AC2: no se habilita pago, así que el paso del pago ni siquiera queda «actual».
    assert resumen(pasos) == "CCCCP"


def test_la_primera_vez_que_ocurre_un_paso_fija_su_fecha(
    entrada: Callable[..., EntradaHistorial], ahora: datetime
) -> None:
    historial = historial_eva01(entrada, ahora)
    repetida = entrada("EVIDENCIAS_VALIDADAS", ahora, actor_rol="SISTEMA")
    pasos = pasos_tramite(EstadoSiniestro.EN_EVALUACION, [repetida, *historial])
    assert pasos[1].ocurrido_en == historial[1].ocurrido_en


def test_las_acciones_desconocidas_no_mueven_el_tramite(
    entrada: Callable[..., EntradaHistorial], ahora: datetime
) -> None:
    historial = [*historial_eva01(entrada, ahora), entrada("COMENTARIO_INTERNO", ahora)]
    assert resumen(pasos_tramite(EstadoSiniestro.EN_EVALUACION, historial)) == "CCAPP"


def test_todos_los_pasos_cumplidos_no_dejan_paso_actual(
    entrada: Callable[..., EntradaHistorial], ahora: datetime
) -> None:
    historial = historial_eva01(entrada, ahora)
    historial.append(
        entrada(
            "RECHAZADO",
            ahora,
            estado_anterior=EstadoSiniestro.EN_EVALUACION,
            estado_nuevo=EstadoSiniestro.RECHAZADO,
        )
    )
    # W20, AC2: un rechazo no habilita pago; el trámite terminó y ningún paso queda «actual».
    assert resumen(pasos_tramite(EstadoSiniestro.RECHAZADO, historial)) == "CCCCP"
