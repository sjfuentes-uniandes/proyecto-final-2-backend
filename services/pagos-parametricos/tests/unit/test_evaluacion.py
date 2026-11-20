"""UT-W21-02 (N1): clases de equivalencia y valores límite de ventana, zona y umbral, y la
precedencia de las razones (HU-W21, DS3-21)."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from pagos_parametricos.domain.parametrico.evaluacion import evaluar
from pagos_parametricos.domain.parametrico.modelos import (
    Cobertura,
    DecisionEvento,
    DecisionPoliza,
    EstadoCobertura,
    EventoObservado,
    Razon,
    Regla,
)
from pagos_parametricos.domain.parametrico.ventana import ventana_de_vigencia

DESDE = datetime(2026, 9, 1, 5, tzinfo=UTC)
HASTA = datetime(2027, 9, 1, 5, tzinfo=UTC)
EVENTO_ID = UUID("bafc2b26-7c88-5ae3-b5ac-26fdf5427a09")


def _regla(**cambios: Any) -> Regla:
    base: dict[str, Any] = {
        "tipo_evento": "LLUVIA_ACUMULADA",
        "zona": "11001",
        "umbral": Decimal(120),
        "unidad": "mm",
        "valor_pago": Decimal("1400000.00"),
        "moneda": "COP",
        "version_regla": "1",
        "vigente_desde": DESDE,
        "vigente_hasta": HASTA,
    }
    base.update(cambios)
    return Regla(**base)


def _cobertura(
    numero: int = 1, estado: EstadoCobertura = EstadoCobertura.ACTIVA, **cambios: Any
) -> Cobertura:
    return Cobertura(
        poliza_id=UUID(int=numero),
        estado=estado,
        regla=_regla(**cambios),
        ultimo_evento_en=DESDE,
    )


def _evento(**cambios: Any) -> EventoObservado:
    base: dict[str, Any] = {
        "evento_id": EVENTO_ID,
        "fuente": "IDEAM-SIM",
        "id_externo": "IDEAM-1",
        "evento_origen": "EVT-1",
        "tipo_evento": "LLUVIA_ACUMULADA",
        "valor_observado": Decimal(135),
        "unidad": "mm",
        "zona": "11001",
        "observado_en": datetime(2026, 11, 10, 14, tzinfo=UTC),
    }
    base.update(cambios)
    return EventoObservado(**base)


# --- AC1: activa -----------------------------------------------------------------------------------
def test_un_evento_dentro_de_todo_activa_y_registra_la_regla_aplicada() -> None:
    resultado = evaluar(_evento(), [_cobertura()])
    assert (resultado.decision, resultado.razon) == (DecisionEvento.ACTIVA, None)
    assert (resultado.evaluadas, resultado.activaciones) == (1, 1)
    [evaluacion] = resultado.evaluaciones
    assert evaluacion.decision is DecisionPoliza.ACTIVA and evaluacion.razon is None
    # La regla aplicada y lo que W22 necesita para pagar.
    assert evaluacion.version_regla == "1"
    assert (evaluacion.valor_pago, evaluacion.moneda) == (Decimal("1400000.00"), "COP")


# --- Valores límite del umbral ---------------------------------------------------------------------
@pytest.mark.parametrize(
    ("valor", "activa"),
    [
        ("0", False),
        ("119", False),
        ("119.999", False),
        ("120", True),
        ("120.001", True),
        ("121", True),
        ("999999999.999", True),
    ],
)
def test_justo_en_el_umbral_activa_y_una_unidad_abajo_no(valor: str, activa: bool) -> None:
    [evaluacion] = evaluar(_evento(valor_observado=Decimal(valor)), [_cobertura()]).evaluaciones
    assert (evaluacion.decision is DecisionPoliza.ACTIVA) is activa
    assert evaluacion.razon is (None if activa else Razon.BAJO_UMBRAL)


def test_el_umbral_se_compara_como_decimal_no_como_texto() -> None:
    """ "1000" ≥ "120" como número, aunque como texto sea menor."""
    [evaluacion] = evaluar(_evento(valor_observado=Decimal(1000)), [_cobertura()]).evaluaciones
    assert evaluacion.decision is DecisionPoliza.ACTIVA
    [evaluacion] = evaluar(_evento(valor_observado=Decimal("99.9")), [_cobertura()]).evaluaciones
    assert evaluacion.razon is Razon.BAJO_UMBRAL


# --- Valores límite de la ventana ------------------------------------------------------------------
@pytest.mark.parametrize(
    ("instante", "dentro"),
    [
        (DESDE - timedelta(microseconds=1), False),
        (DESDE, True),  # justo en vigenteDesde: incluido
        (DESDE + timedelta(microseconds=1), True),
        (HASTA - timedelta(microseconds=1), True),
        (HASTA, False),  # justo en vigenteHasta: excluido
        (HASTA + timedelta(days=30), False),
        (DESDE - timedelta(days=365), False),
    ],
)
def test_la_ventana_incluye_el_inicio_y_excluye_el_fin(instante: datetime, dentro: bool) -> None:
    [evaluacion] = evaluar(_evento(observado_en=instante), [_cobertura()]).evaluaciones
    assert (evaluacion.razon is not Razon.FUERA_DE_VENTANA) is dentro
    assert (evaluacion.decision is DecisionPoliza.ACTIVA) is dentro


def test_la_ventana_sale_de_la_vigencia_inclusiva_de_la_poliza() -> None:
    desde, hasta = ventana_de_vigencia(date(2026, 11, 2), date(2027, 11, 1))
    regla = _regla(vigente_desde=desde, vigente_hasta=hasta)
    cobertura = Cobertura(UUID(int=1), EstadoCobertura.ACTIVA, regla, DESDE)
    un_segundo = timedelta(seconds=1)
    for instante, activa in [
        (datetime(2026, 11, 2, 5, tzinfo=UTC) - un_segundo, False),
        (datetime(2026, 11, 2, 5, tzinfo=UTC), True),
        (datetime(2027, 11, 2, 5, tzinfo=UTC) - un_segundo, True),  # el último día, inclusive
        (datetime(2027, 11, 2, 5, tzinfo=UTC), False),
    ]:
        [e] = evaluar(_evento(observado_en=instante), [cobertura]).evaluaciones
        assert (e.decision is DecisionPoliza.ACTIVA) is activa, instante


# --- Precedencia de las razones --------------------------------------------------------------------
def test_poliza_no_activa_gana_a_todas_las_demas() -> None:
    cobertura = _cobertura(estado=EstadoCobertura.CANCELADA, zona="05001")
    [evaluacion] = evaluar(
        _evento(observado_en=HASTA, valor_observado=Decimal(1)), [cobertura]
    ).evaluaciones
    assert evaluacion.razon is Razon.POLIZA_NO_ACTIVA


def test_fuera_de_ventana_gana_a_fuera_de_zona_y_bajo_umbral() -> None:
    [evaluacion] = evaluar(
        _evento(observado_en=HASTA, zona="05001", valor_observado=Decimal(1)), [_cobertura()]
    ).evaluaciones
    assert evaluacion.razon is Razon.FUERA_DE_VENTANA


def test_fuera_de_zona_gana_a_bajo_umbral() -> None:
    [evaluacion] = evaluar(
        _evento(zona="05001", valor_observado=Decimal(1)), [_cobertura()]
    ).evaluaciones
    assert evaluacion.razon is Razon.FUERA_DE_ZONA


def test_bajo_umbral_es_la_ultima_razon() -> None:
    [evaluacion] = evaluar(_evento(valor_observado=Decimal(1)), [_cobertura()]).evaluaciones
    assert evaluacion.razon is Razon.BAJO_UMBRAL


# --- A nivel de evento -----------------------------------------------------------------------------
def test_sin_candidatas_es_sin_polizas_en_la_zona() -> None:
    resultado = evaluar(_evento(), [])
    assert (resultado.decision, resultado.razon) == (
        DecisionEvento.SIN_POLIZAS,
        Razon.SIN_POLIZAS_EN_ZONA,
    )
    assert (resultado.evaluadas, resultado.activaciones, resultado.evaluaciones) == (0, 0, ())


def test_una_cobertura_de_otro_tipo_no_es_candidata() -> None:
    otra = _cobertura(tipo_evento="RETRASO_VUELO", unidad="min")
    resultado = evaluar(_evento(), [otra])
    assert resultado.decision is DecisionEvento.SIN_POLIZAS


def test_una_cancelada_sin_regla_no_se_puede_comparar_y_no_es_candidata() -> None:
    sin_regla = Cobertura(UUID(int=9), EstadoCobertura.CANCELADA, None, DESDE)
    assert evaluar(_evento(), [sin_regla]).decision is DecisionEvento.SIN_POLIZAS


def test_con_varias_polizas_activa_si_al_menos_una_activa() -> None:
    resultado = evaluar(
        _evento(),
        [
            _cobertura(3, vigente_desde=DESDE - timedelta(days=400), vigente_hasta=DESDE),
            _cobertura(2, umbral=Decimal(200)),
            _cobertura(1),
        ],
    )
    assert resultado.decision is DecisionEvento.ACTIVA
    assert (resultado.evaluadas, resultado.activaciones) == (3, 1)
    # Ordenadas por póliza, sin depender del orden en que llegaron.
    assert [e.poliza_id.int for e in resultado.evaluaciones] == [1, 2, 3]
    assert [e.razon for e in resultado.evaluaciones] == [
        None,
        Razon.BAJO_UMBRAL,
        Razon.FUERA_DE_VENTANA,
    ]


def test_si_ninguna_activa_es_no_activa_sin_razon_de_evento() -> None:
    resultado = evaluar(_evento(valor_observado=Decimal(1)), [_cobertura(1), _cobertura(2)])
    assert (resultado.decision, resultado.razon) == (DecisionEvento.NO_ACTIVA, None)
    assert (resultado.evaluadas, resultado.activaciones) == (2, 0)


def test_cada_evaluacion_guarda_la_version_de_la_regla_de_su_poliza() -> None:
    resultado = evaluar(
        _evento(), [_cobertura(1, version_regla="1"), _cobertura(2, version_regla="2")]
    )
    assert [e.version_regla for e in resultado.evaluaciones] == ["1", "2"]


# --- La matriz del archivo de datos, contra un evaluador independiente ----------------------------
def test_cada_caso_del_archivo_de_datos_da_lo_esperado(
    datos: dict[str, Any], casos: dict[str, dict[str, Any]]
) -> None:
    """Las coberturas salen de los `PolizaEmitida` sintéticos (con la ventana de su vigencia) y las
    candidatas, del mismo filtro que el índice `(tipo_evento, zona)` sobre las ACTIVA."""
    from datetime import date as _fecha

    coberturas: dict[str, Cobertura] = {}
    claves: dict[UUID, str] = {}
    for entrada in datos["coberturas"]:
        payload = entrada["sobre"]["payload"]
        regla = payload["coberturaParametrica"]
        inicio, fin = ventana_de_vigencia(
            _fecha.fromisoformat(payload["vigencia"]["desde"]),
            _fecha.fromisoformat(payload["vigencia"]["hasta"]),
        )
        poliza_id = UUID(payload["polizaId"])
        claves[poliza_id] = entrada["clave"]
        coberturas[entrada["clave"]] = Cobertura(
            poliza_id,
            EstadoCobertura.ACTIVA,
            Regla(
                tipo_evento=regla["tipoEvento"],
                zona=regla["zona"],
                umbral=Decimal(regla["umbral"]),
                unidad=regla["unidad"],
                valor_pago=Decimal(regla["valorPago"]),
                moneda=regla["moneda"],
                version_regla=regla["versionRegla"],
                vigente_desde=inicio,
                vigente_hasta=fin,
            ),
            datetime(2026, 11, 1, tzinfo=UTC),
        )
    verificados = 0
    for caso in casos.values():
        esperado = caso["esperado"]["evaluacion"]
        if esperado is None:
            continue
        cuerpo = caso["envio"]["cuerpo"]
        evento = _evento(
            fuente=caso["envio"]["fuente"],
            tipo_evento=cuerpo["tipoEvento"],
            valor_observado=Decimal(cuerpo["valorObservado"]),
            unidad=cuerpo["unidad"],
            zona=cuerpo["zona"],
            observado_en=datetime.fromisoformat(cuerpo["observadoEn"]),
        )
        candidatas = [
            c
            for c in coberturas.values()
            if c.regla and (c.regla.tipo_evento, c.regla.zona) == (evento.tipo_evento, evento.zona)
        ]
        resultado = evaluar(evento, candidatas)
        assert resultado.decision.value == esperado["decision"], caso["clave"]
        assert (resultado.razon.value if resultado.razon else None) == esperado["razonEvento"]
        assert (resultado.evaluadas, resultado.activaciones) == (
            esperado["evaluadas"],
            esperado["activaciones"],
        ), caso["clave"]
        por_poliza = {
            claves[e.poliza_id]: {
                "decision": e.decision.value,
                "razon": e.razon.value if e.razon else None,
            }
            for e in resultado.evaluaciones
        }
        assert por_poliza == esperado["porPoliza"], caso["clave"]
        verificados += 1
    assert verificados == 8
