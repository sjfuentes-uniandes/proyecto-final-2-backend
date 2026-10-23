"""UT-W13-01 y UT-W13-02: tabla de decisión del motor (DS2-06), límites y determinismo.

Las reglas son las de la semilla 2026.10.1 de catalogo y los insumos, los casos sintéticos de
contracts/datos-sinteticos/cotizaciones-suscripcion.json.
"""

import dataclasses
from decimal import Decimal
from itertools import product
from typing import Any

import pytest
from polizas.domain.suscripcion.modelos import (
    InsumosSuscripcion,
    NivelRiesgo,
    PerfilInsumos,
    Regla,
    ResultadoDecision,
    ResultadoRegla,
    TipoRegla,
    Visibilidad,
)
from polizas.domain.suscripcion.motor import cumple, evaluar, evaluar_regla, valor_del_caso


def _con(base: InsumosSuscripcion, **cambios: Any) -> InsumosSuscripcion:
    return dataclasses.replace(base, **cambios)


def _con_perfil(base: InsumosSuscripcion, **cambios: Any) -> InsumosSuscripcion:
    assert base.perfil is not None
    return dataclasses.replace(base, perfil=dataclasses.replace(base.perfil, **cambios))


def test_casos_sinteticos(id_caso_decidido: str, sinteticos: Any) -> None:
    """Cada caso de los mockups da el resultado, la regla determinante, los motivos y el
    resultado por regla esperados."""
    esperado = sinteticos.caso(id_caso_decidido)["esperado"]
    evaluacion = evaluar(sinteticos.insumos(id_caso_decidido), sinteticos.reglas().reglas)
    assert evaluacion.resultado.value == esperado["resultado"]
    assert evaluacion.regla_determinante == esperado["reglaDeterminante"]
    assert list(evaluacion.motivos) == esperado["motivos"]
    assert {r.regla.codigo: r.resultado.value for r in evaluacion.reglas} == esperado["reglas"]


def test_aprobada_seis_reglas_cumplen_en_orden(sinteticos: Any) -> None:
    reglas = sinteticos.reglas().reglas
    evaluacion = evaluar(sinteticos.insumos("S01"), tuple(reversed(reglas)))
    assert evaluacion.resultado is ResultadoDecision.APROBADA
    assert [r.regla.codigo for r in evaluacion.reglas] == [
        "SUS-001",
        "SUS-004",
        "SUS-007",
        "SUS-011",
        "SUS-015",
        "SUS-019",
    ]
    assert [r.valor_caso for r in evaluacion.reglas] == [
        "36",
        "180000000",
        "BAJO",
        "0.92",
        "0",
        "180",
    ]
    assert evaluacion.reglas_no_cumplidas == ()


# Tabla de decisión: cada regla de la semilla con un valor que cumple, uno que no y uno ausente.
CUMPLE_NO_CUMPLE: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "SUS-001": ({"edad": 40}, {"edad": 70}),
    "SUS-004": ({"suma_asegurada": Decimal(1)}, {"suma_asegurada": Decimal(600_000_000)}),
    "SUS-007": ({"nivel_riesgo": NivelRiesgo.MEDIO}, {"nivel_riesgo": NivelRiesgo.ALTO}),
    "SUS-011": ({"confianza": Decimal("0.99")}, {"confianza": Decimal("0.10")}),
    "SUS-015": ({"coincidencias_listas_restrictivas": 0}, {"coincidencias_listas_restrictivas": 2}),
    "SUS-019": ({"plazo_credito_meses": 120}, {"plazo_credito_meses": 300}),
}
AUSENTE: dict[str, dict[str, Any]] = {
    "SUS-007": {"nivel_riesgo": None},
    "SUS-011": {"perfil": None},
    "SUS-015": {"coincidencias_listas_restrictivas": None},
}
DEL_PERFIL = {"nivel_riesgo", "confianza"}


def _aplicar(base: InsumosSuscripcion, cambios: dict[str, Any]) -> InsumosSuscripcion:
    del_perfil = {k: v for k, v in cambios.items() if k in DEL_PERFIL}
    resto = {k: v for k, v in cambios.items() if k not in DEL_PERFIL}
    resultado = _con(base, **resto) if resto else base
    return _con_perfil(resultado, **del_perfil) if del_perfil else resultado


@pytest.mark.parametrize(
    ("codigo", "estado"),
    [
        *product(CUMPLE_NO_CUMPLE, ["CUMPLE", "NO_CUMPLE"]),
        *product(AUSENTE, ["NO_EVALUABLE"]),
    ],
)
def test_tabla_de_decision_por_regla(codigo: str, estado: str, sinteticos: Any) -> None:
    """Cada regla × cumple / no cumple / no evaluable, y el resultado según su tipo (DS2-06)."""
    cumple_, no_cumple = CUMPLE_NO_CUMPLE[codigo]
    cambios = {"CUMPLE": cumple_, "NO_CUMPLE": no_cumple, "NO_EVALUABLE": AUSENTE.get(codigo)}[
        estado
    ]
    assert cambios is not None
    evaluacion = evaluar(_aplicar(sinteticos.insumos("S01"), cambios), sinteticos.reglas().reglas)
    resultados = {r.regla.codigo: r.resultado.value for r in evaluacion.reglas}
    assert resultados[codigo] == estado
    regla = sinteticos.regla(codigo)
    if estado == "CUMPLE":
        assert evaluacion.resultado is ResultadoDecision.APROBADA
    elif estado == "NO_CUMPLE" and regla.tipo is TipoRegla.EXCLUYENTE:
        assert evaluacion.resultado is ResultadoDecision.RECHAZADA
        assert evaluacion.regla_determinante == codigo
        assert evaluacion.motivos == (regla.motivo_cliente,)
    else:
        # REVISION incumplida o cualquier regla no evaluable: nunca rechaza ni aprueba sin dato.
        assert evaluacion.resultado is ResultadoDecision.REVISION_REQUERIDA
        assert evaluacion.regla_determinante is None


def test_la_excluyente_tiene_precedencia_sobre_la_revision(sinteticos: Any) -> None:
    """S04: SUS-007 (REVISION) y SUS-015 (EXCLUYENTE) fallan → RECHAZADA por SUS-015."""
    evaluacion = evaluar(sinteticos.insumos("S04"), sinteticos.reglas().reglas)
    assert evaluacion.resultado is ResultadoDecision.RECHAZADA
    assert evaluacion.regla_determinante == "SUS-015"
    assert evaluacion.reglas_no_cumplidas == ("SUS-007", "SUS-015")


def test_la_determinante_es_la_primera_excluyente_por_orden(sinteticos: Any) -> None:
    entrada = _con(sinteticos.insumos("S01"), edad=68, plazo_credito_meses=300)
    evaluacion = evaluar(entrada, tuple(reversed(sinteticos.reglas().reglas)))
    assert evaluacion.regla_determinante == "SUS-001"
    assert evaluacion.motivos == ("EDAD_FUERA_DE_RANGO", "PLAZO_FUERA_DE_RANGO")


def test_una_excluyente_no_evaluable_enruta_a_revision_sin_motivo_reservado(
    sinteticos: Any,
) -> None:
    """S08 (sin perfil): SUS-015 es RESERVADA; no evaluable no rechaza ni dice NO_ELEGIBLE."""
    evaluacion = evaluar(sinteticos.insumos("S08"), sinteticos.reglas().reglas)
    assert evaluacion.resultado is ResultadoDecision.REVISION_REQUERIDA
    assert evaluacion.motivos == ("PERFIL_REQUIERE_REVISION",)
    assert "SUS-015" in evaluacion.reglas_no_cumplidas


@pytest.mark.parametrize(
    ("campo", "valor", "esperado"),
    [
        # SUS-001 edad entre 18 y 65, inclusive.
        ("edad", 17, "NO_CUMPLE"),
        ("edad", 18, "CUMPLE"),
        ("edad", 65, "CUMPLE"),
        ("edad", 66, "NO_CUMPLE"),
        # SUS-004 suma ≤ 500.000.000.
        ("suma_asegurada", Decimal(500_000_000), "CUMPLE"),
        ("suma_asegurada", Decimal(500_000_001), "NO_CUMPLE"),
        # SUS-019 plazo ≤ 240.
        ("plazo_credito_meses", 240, "CUMPLE"),
        ("plazo_credito_meses", 241, "NO_CUMPLE"),
        # SUS-011 confianza ≥ 0,70.
        ("confianza", Decimal("0.70"), "CUMPLE"),
        ("confianza", Decimal("0.7"), "CUMPLE"),
        ("confianza", Decimal("0.69"), "NO_CUMPLE"),
    ],
)
def test_limites(campo: str, valor: Any, esperado: str, sinteticos: Any) -> None:
    """UT-W13-02: los valores en el borde se comportan según el operador."""
    codigo = {
        "edad": "SUS-001",
        "suma_asegurada": "SUS-004",
        "plazo_credito_meses": "SUS-019",
        "confianza": "SUS-011",
    }[campo]
    entrada = _aplicar(sinteticos.insumos("S01"), {campo: valor})
    assert evaluar_regla(sinteticos.regla(codigo), entrada).resultado.value == esperado


def _regla_con(operador: str, valor: str | tuple[str, ...], campo: str = "edad") -> Regla:
    return Regla(
        "SUS-900", 9, TipoRegla.REVISION, "x", campo, operador, valor, Visibilidad.EXPLICABLE, "X"
    )


@pytest.mark.parametrize(
    ("operador", "valor", "caso_", "esperado"),
    [
        (">=", "18", Decimal(18), True),
        (">=", "18", Decimal(17), False),
        ("==", "0", Decimal(0), True),
        ("==", "0", Decimal(1), False),
        ("!=", "0", Decimal(1), True),
        ("en", ("120", "240"), Decimal(240), True),
        ("en", ("120", "240"), Decimal(180), False),
        ("entre", ("1", "2"), Decimal("1.5"), True),
        ("==", "ALTO", "ALTO", True),
        ("!=", "ALTO", "BAJO", True),
        ("en", ("BAJO", "MEDIO"), "MEDIO", True),
        ("en", ("BAJO", "MEDIO"), "ALTO", False),
    ],
)
def test_operadores(
    operador: str, valor: str | tuple[str, ...], caso_: Decimal | str, esperado: bool
) -> None:
    assert cumple(_regla_con(operador, valor), caso_) is esperado


@pytest.mark.parametrize(
    ("operador", "valor", "caso_"),
    [
        ("entre", "18", Decimal(1)),
        (">", "18", Decimal(1)),
        (">=", "MEDIO", "ALTO"),
        ("entre", ("BAJO", "ALTO"), "MEDIO"),
    ],
)
def test_una_regla_fuera_del_contrato_falla(
    operador: str, valor: str | tuple[str, ...], caso_: Decimal | str
) -> None:
    """catalogo valida al publicar; si una regla llega fuera del contrato, el motor no adivina."""
    with pytest.raises(ValueError, match="fuera del contrato"):
        cumple(_regla_con(operador, valor), caso_)


def test_un_campo_desconocido_no_es_evaluable(sinteticos: Any) -> None:
    base = sinteticos.insumos("S01")
    assert valor_del_caso(base, "ingreso_mensual") is None
    evaluada = evaluar_regla(_regla_con(">=", "1", campo="ingreso_mensual"), base)
    assert evaluada.resultado is ResultadoRegla.NO_EVALUABLE


def test_perfil_sin_nivel_y_sin_perfil(sinteticos: Any) -> None:
    sin_nivel = sinteticos.insumos("S07")
    assert valor_del_caso(sin_nivel, "nivel_riesgo") is None
    assert valor_del_caso(sin_nivel, "confianza_perfil") == Decimal("0.92")
    sin_perfil = sinteticos.insumos("S08")
    assert valor_del_caso(sin_perfil, "confianza_perfil") is None
    assert isinstance(sinteticos.insumos("S01").perfil, PerfilInsumos)


def test_determinismo(id_caso_decidido: str, sinteticos: Any) -> None:
    """Mismo insumo y misma versión de reglas → misma evaluación, en cualquier orden de reglas."""
    reglas = sinteticos.reglas().reglas
    a = evaluar(sinteticos.insumos(id_caso_decidido), reglas)
    b = evaluar(sinteticos.insumos(id_caso_decidido), tuple(reversed(reglas)))
    assert a == b
