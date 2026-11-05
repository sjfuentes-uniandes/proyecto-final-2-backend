"""Verificación de cobertura y soportes pendientes (DS3-05)."""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import pytest
from siniestros.domain.siniestro.cobertura import (
    COLOMBIA,
    SoportePendiente,
    Verificacion,
    soportes_pendientes,
    verificar_cobertura,
)
from siniestros.domain.siniestro.modelos import (
    Amparo,
    CodigoVerificacion,
    CopiaPoliza,
    EstadoSoporte,
    EventoReclamado,
    Evidencia,
    ResultadoVerificacion,
    TipoSoporte,
    Vigencia,
)

CUMPLE = ResultadoVerificacion.CUMPLE
NO_CUMPLE = ResultadoVerificacion.NO_CUMPLE
PARCIAL = ResultadoVerificacion.PARCIAL
NO_EVALUABLE = ResultadoVerificacion.NO_EVALUABLE
REQUERIDOS = (
    TipoSoporte.FACTURA_COMPRA,
    TipoSoporte.DOCUMENTO_IDENTIDAD,
    TipoSoporte.CERTIFICADO_REPARACION,
)


@pytest.fixture
def evento(ahora: datetime) -> EventoReclamado:
    reportado = ahora - timedelta(hours=14)
    return EventoReclamado("DANO_ACCIDENTAL", reportado - timedelta(minutes=41), reportado)


def por_codigo(verificaciones: tuple[Verificacion, ...]) -> dict[CodigoVerificacion, Verificacion]:
    return {v.codigo: v for v in verificaciones}


def test_siempre_son_seis_en_el_orden_del_contrato(
    copia: Callable[..., CopiaPoliza], evento: EventoReclamado
) -> None:
    resultado = verificar_cobertura(copia(), evento, [], REQUERIDOS)
    assert [v.codigo for v in resultado] == list(CodigoVerificacion)


def test_el_caso_h03_cumple_todo_menos_un_soporte(
    copia: Callable[..., CopiaPoliza],
    evento: EventoReclamado,
    soporte: Callable[[TipoSoporte], Evidencia],
) -> None:
    archivos = [soporte(TipoSoporte.FACTURA_COMPRA), soporte(TipoSoporte.DOCUMENTO_IDENTIDAD)]
    r = por_codigo(verificar_cobertura(copia(), evento, archivos, REQUERIDOS))
    assert {c: v.resultado for c, v in r.items()} == {
        CodigoVerificacion.VIGENCIA: CUMPLE,
        CodigoVerificacion.AMPARO: CUMPLE,
        CodigoVerificacion.CARENCIA: CUMPLE,
        CodigoVerificacion.PLAZO_AVISO: CUMPLE,
        CodigoVerificacion.PRIMAS: CUMPLE,
        CodigoVerificacion.SOPORTES: PARCIAL,
    }
    assert r[CodigoVerificacion.SOPORTES].detalle == {"presentes": "2", "requeridos": "3"}


def test_el_detalle_son_parametros_en_texto(
    copia: Callable[..., CopiaPoliza], evento: EventoReclamado
) -> None:
    r = por_codigo(verificar_cobertura(copia(), evento, [], REQUERIDOS))
    # Las 19:19 del 4 de noviembre en Colombia (00:19 UTC del 5).
    dias = (date(2026, 11, 4) - date(2026, 5, 9)).days
    assert r[CodigoVerificacion.VIGENCIA].detalle == {
        "fechaEvento": "2026-11-04",
        "desde": "2026-05-09",
        "hasta": "2027-05-09",
    }
    assert r[CodigoVerificacion.AMPARO].detalle == {"amparo": "DANO_ACCIDENTAL"}
    assert r[CodigoVerificacion.CARENCIA].detalle == {
        "diasDesdeInicio": str(dias),
        "carenciaDias": "30",
    }
    assert r[CodigoVerificacion.PLAZO_AVISO].detalle == {
        "minutosDesdeEvento": "41",
        "plazoAvisoDias": "30",
    }
    assert r[CodigoVerificacion.PRIMAS].detalle == {"primasAlDia": "true"}


def test_una_poliza_vencida_al_momento_del_evento_no_cumple_la_vigencia(
    copia: Callable[..., CopiaPoliza], evento: EventoReclamado
) -> None:
    vencida = copia(vigencia=Vigencia(date(2025, 5, 9), date(2026, 5, 9)))
    r = por_codigo(verificar_cobertura(vencida, evento, [], REQUERIDOS))
    assert r[CodigoVerificacion.VIGENCIA].resultado is NO_CUMPLE


@pytest.mark.parametrize(
    ("instante", "cumple"),
    [
        # 2026-05-09 23:30 en Colombia = 2026-05-10 04:30 UTC: todavía es el último día.
        (datetime(2026, 5, 10, 4, 30, tzinfo=UTC), True),
        # 2026-05-10 00:30 en Colombia: ya no.
        (datetime(2026, 5, 10, 5, 30, tzinfo=UTC), False),
    ],
)
def test_la_vigencia_se_compara_por_dia_de_colombia(
    copia: Callable[..., CopiaPoliza], ahora: datetime, instante: datetime, cumple: bool
) -> None:
    poliza = copia(vigencia=Vigencia(date(2025, 5, 10), date(2026, 5, 9)), carencia_dias=0)
    evento = EventoReclamado("DANO_ACCIDENTAL", instante, instante + timedelta(hours=1))
    resultado = por_codigo(verificar_cobertura(poliza, evento, [], REQUERIDOS))
    assert resultado[CodigoVerificacion.VIGENCIA].resultado is (CUMPLE if cumple else NO_CUMPLE)
    assert instante.astimezone(COLOMBIA).date() in (date(2026, 5, 9), date(2026, 5, 10))


def test_la_vigencia_incluye_el_primer_y_el_ultimo_dia(
    copia: Callable[..., CopiaPoliza],
) -> None:
    poliza = copia(vigencia=Vigencia(date(2026, 5, 9), date(2026, 5, 9)), carencia_dias=0)
    mediodia = datetime(2026, 5, 9, 17, 0, tzinfo=UTC)
    evento = EventoReclamado("DANO_ACCIDENTAL", mediodia, mediodia + timedelta(hours=1))
    r = por_codigo(verificar_cobertura(poliza, evento, [], REQUERIDOS))
    assert r[CodigoVerificacion.VIGENCIA].resultado is CUMPLE
    assert r[CodigoVerificacion.CARENCIA].resultado is CUMPLE


def test_un_amparo_no_contratado_no_cumple(
    copia: Callable[..., CopiaPoliza], evento: EventoReclamado
) -> None:
    robo = EventoReclamado("ROBO", evento.fecha_evento, evento.reportado_en)
    r = por_codigo(verificar_cobertura(copia(), robo, [], REQUERIDOS))
    assert r[CodigoVerificacion.AMPARO].resultado is NO_CUMPLE
    assert r[CodigoVerificacion.AMPARO].detalle == {"amparo": "ROBO"}


def test_la_carencia_se_cuenta_en_dias_desde_el_inicio(
    copia: Callable[..., CopiaPoliza], ahora: datetime
) -> None:
    poliza = copia(vigencia=Vigencia(date(2026, 11, 1), date(2027, 11, 1)), carencia_dias=30)
    evento = EventoReclamado("DANO_ACCIDENTAL", ahora - timedelta(days=2), ahora)
    r = por_codigo(verificar_cobertura(poliza, evento, [], REQUERIDOS))
    assert r[CodigoVerificacion.CARENCIA].resultado is NO_CUMPLE
    assert r[CodigoVerificacion.CARENCIA].detalle == {
        "diasDesdeInicio": "2",
        "carenciaDias": "30",
    }


def test_un_evento_justo_al_cumplir_la_carencia_cumple(
    copia: Callable[..., CopiaPoliza],
) -> None:
    poliza = copia(vigencia=Vigencia(date(2026, 10, 1), date(2027, 10, 1)), carencia_dias=30)
    dia_30 = datetime(2026, 10, 31, 17, 0, tzinfo=UTC)
    evento = EventoReclamado("DANO_ACCIDENTAL", dia_30, dia_30 + timedelta(hours=1))
    r = por_codigo(verificar_cobertura(poliza, evento, [], REQUERIDOS))
    assert r[CodigoVerificacion.CARENCIA].resultado is CUMPLE


def test_un_evento_antes_del_inicio_no_cumple_la_carencia(
    copia: Callable[..., CopiaPoliza], ahora: datetime
) -> None:
    poliza = copia(vigencia=Vigencia(date(2026, 11, 10), date(2027, 11, 10)), carencia_dias=0)
    evento = EventoReclamado("DANO_ACCIDENTAL", ahora - timedelta(days=1), ahora)
    r = por_codigo(verificar_cobertura(poliza, evento, [], REQUERIDOS))
    assert r[CodigoVerificacion.CARENCIA].resultado is NO_CUMPLE


def test_el_aviso_fuera_de_plazo_no_cumple(
    copia: Callable[..., CopiaPoliza], ahora: datetime
) -> None:
    evento = EventoReclamado("DANO_ACCIDENTAL", ahora - timedelta(days=31), ahora)
    r = por_codigo(verificar_cobertura(copia(plazo_aviso_dias=30), evento, [], REQUERIDOS))
    assert r[CodigoVerificacion.PLAZO_AVISO].resultado is NO_CUMPLE
    assert r[CodigoVerificacion.PLAZO_AVISO].detalle["minutosDesdeEvento"] == str(31 * 24 * 60)


def test_el_aviso_justo_en_el_limite_cumple(
    copia: Callable[..., CopiaPoliza], ahora: datetime
) -> None:
    evento = EventoReclamado("DANO_ACCIDENTAL", ahora - timedelta(days=30), ahora)
    r = por_codigo(verificar_cobertura(copia(plazo_aviso_dias=30), evento, [], REQUERIDOS))
    assert r[CodigoVerificacion.PLAZO_AVISO].resultado is CUMPLE


def test_las_primas_vencidas_no_cumplen(
    copia: Callable[..., CopiaPoliza], evento: EventoReclamado
) -> None:
    r = por_codigo(verificar_cobertura(copia(primas_al_dia=False), evento, [], REQUERIDOS))
    assert r[CodigoVerificacion.PRIMAS].resultado is NO_CUMPLE
    assert r[CodigoVerificacion.PRIMAS].detalle == {"primasAlDia": "false"}


def test_sin_soportes_no_cumple_y_con_todos_cumple(
    copia: Callable[..., CopiaPoliza],
    evento: EventoReclamado,
    soporte: Callable[[TipoSoporte], Evidencia],
) -> None:
    ninguno = por_codigo(verificar_cobertura(copia(), evento, [], REQUERIDOS))
    assert ninguno[CodigoVerificacion.SOPORTES].resultado is NO_CUMPLE
    todos = por_codigo(
        verificar_cobertura(copia(), evento, [soporte(t) for t in REQUERIDOS], REQUERIDOS)
    )
    assert todos[CodigoVerificacion.SOPORTES].resultado is CUMPLE
    assert todos[CodigoVerificacion.SOPORTES].detalle == {"presentes": "3", "requeridos": "3"}


def test_un_soporte_que_no_es_requerido_no_cuenta(
    copia: Callable[..., CopiaPoliza],
    evento: EventoReclamado,
    soporte: Callable[[TipoSoporte], Evidencia],
) -> None:
    r = por_codigo(
        verificar_cobertura(copia(), evento, [soporte(TipoSoporte.PERITAJE)], REQUERIDOS)
    )
    assert r[CodigoVerificacion.SOPORTES].resultado is NO_CUMPLE


def test_las_fotos_no_cubren_soportes(
    copia: Callable[..., CopiaPoliza],
    evento: EventoReclamado,
    evidencia: Callable[..., Evidencia],
) -> None:
    r = por_codigo(verificar_cobertura(copia(), evento, [evidencia()], REQUERIDOS))
    assert r[CodigoVerificacion.SOPORTES].detalle["presentes"] == "0"


def test_los_requeridos_repetidos_se_cuentan_una_vez(
    copia: Callable[..., CopiaPoliza],
    evento: EventoReclamado,
    soporte: Callable[[TipoSoporte], Evidencia],
) -> None:
    repetidos = [TipoSoporte.PERITAJE, TipoSoporte.PERITAJE]
    r = por_codigo(verificar_cobertura(copia(), evento, [soporte(TipoSoporte.PERITAJE)], repetidos))
    assert r[CodigoVerificacion.SOPORTES].resultado is CUMPLE
    assert r[CodigoVerificacion.SOPORTES].detalle == {"presentes": "1", "requeridos": "1"}


def test_sin_soportes_requeridos_no_hay_nada_que_evaluar(
    copia: Callable[..., CopiaPoliza], evento: EventoReclamado
) -> None:
    r = por_codigo(verificar_cobertura(copia(), evento, [], []))
    assert r[CodigoVerificacion.SOPORTES].resultado is NO_EVALUABLE


def test_lo_que_falta_en_la_copia_no_es_evaluable(evento: EventoReclamado) -> None:
    vacia = CopiaPoliza(numero="220-2026-000001", ramo="Dispositivos", moneda="COP")
    r = por_codigo(verificar_cobertura(vacia, evento, [], REQUERIDOS))
    assert {c: v.resultado for c, v in r.items()} == {
        CodigoVerificacion.VIGENCIA: NO_EVALUABLE,
        CodigoVerificacion.AMPARO: NO_EVALUABLE,
        CodigoVerificacion.CARENCIA: NO_EVALUABLE,
        CodigoVerificacion.PLAZO_AVISO: NO_EVALUABLE,
        CodigoVerificacion.PRIMAS: NO_EVALUABLE,
        CodigoVerificacion.SOPORTES: NO_CUMPLE,
    }


def test_la_carencia_sin_dias_declarados_no_es_evaluable(
    copia: Callable[..., CopiaPoliza], evento: EventoReclamado
) -> None:
    r = por_codigo(verificar_cobertura(copia(carencia_dias=None), evento, [], REQUERIDOS))
    assert r[CodigoVerificacion.CARENCIA].resultado is NO_EVALUABLE


def test_los_amparos_vacios_no_son_evaluables(
    copia: Callable[..., CopiaPoliza], evento: EventoReclamado
) -> None:
    r = por_codigo(verificar_cobertura(copia(amparos=()), evento, [], REQUERIDOS))
    assert r[CodigoVerificacion.AMPARO].resultado is NO_EVALUABLE
    otro = copia(amparos=(Amparo("ROBO", "Robo", "1000000.00"),))
    r = por_codigo(verificar_cobertura(otro, evento, [], REQUERIDOS))
    assert r[CodigoVerificacion.AMPARO].resultado is NO_CUMPLE


def test_los_pendientes_son_los_requeridos_sin_archivo_en_orden(
    soporte: Callable[[TipoSoporte], Evidencia],
) -> None:
    pendientes = soportes_pendientes(REQUERIDOS, [soporte(TipoSoporte.DOCUMENTO_IDENTIDAD)])
    assert pendientes == (
        SoportePendiente(TipoSoporte.FACTURA_COMPRA, EstadoSoporte.FALTA),
        SoportePendiente(TipoSoporte.CERTIFICADO_REPARACION, EstadoSoporte.FALTA),
    )


def test_sin_pendientes_cuando_estan_todos(
    soporte: Callable[[TipoSoporte], Evidencia],
) -> None:
    assert soportes_pendientes(REQUERIDOS, [soporte(t) for t in REQUERIDOS]) == ()
    assert soportes_pendientes([], []) == ()
