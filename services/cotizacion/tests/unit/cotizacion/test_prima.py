"""N1 HU-W03 AC1 y AC3: cálculo de la prima (docs/hu-w03-prima.md §3 y §4).

Clases de equivalencia y límites de suma asegurada, plazo y edad; coberturas; mínimos y máximos;
redondeo (modo y escala); valores grandes; y determinismo (mismo desglose y misma huella, sin
depender del contexto decimal global).
"""

import decimal
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from cotizacion.domain.cotizacion.errores import CoberturaNoDisponible, DatosFueraDeRango
from cotizacion.domain.cotizacion.modelos import (
    EntradasPrima,
    FactorVariable,
    Limites,
    Rango,
    Tramo,
)
from cotizacion.domain.cotizacion.prima import (
    CAMPO_COBERTURAS,
    CAMPO_NACIMIENTO,
    CAMPO_PLAZO,
    CAMPO_SUMA,
    calcular_prima,
    desglose_canonico,
    edad_cumplida,
    entradas_canonicas,
    huella_calculo,
    texto,
    validar_entradas,
)
from cotizacion.domain.perfilamiento.modelos import NivelRiesgo

HOY = date(2026, 10, 15)


@pytest.fixture
def producto(semilla_w03: Any) -> Any:
    return semilla_w03[0]()


@pytest.fixture
def reglas(semilla_w03: Any) -> Any:
    return semilla_w03[1]()


def entradas(**cambios: Any) -> EntradasPrima:
    """C01: 180.000.000 COP, 180 meses, 36 años, sin perfil y sin opcionales."""
    base = EntradasPrima(
        suma_asegurada=Decimal(180000000),
        plazo_credito_meses=180,
        edad=36,
        nivel_riesgo=None,
        coberturas_opcionales=(),
    )
    return replace(base, **cambios)


def plano(reglas: Any, **cambios: Any) -> Any:
    """Reglas con todos los factores en 1 y sin límites de prima: aíslan el redondeo."""
    uno = Decimal(1)
    return replace(
        reglas,
        factores_nivel={n: uno for n in ("BAJO", "MEDIO", "ALTO", "SIN_PERFIL")},
        factores_variables=(),
        recargos={},
        prima_minima=Decimal(0),
        prima_maxima=Decimal("1E+15"),
        **cambios,
    )


# --- AC1: cálculo nominal y desglose ------------------------------------------------------


def test_c01_sin_perfil(reglas: Any) -> None:
    """75.600 × SIN_PERFIL 1,15 × EDAD(36) 0,95 × PLAZO(180) 1,00 = 82.593 COP."""
    desglose = calcular_prima(reglas, entradas())
    assert desglose.prima_base == Decimal(75600)
    assert [(f.codigo, f.valor, f.factor) for f in desglose.factores] == [
        ("NIVEL_RIESGO", "SIN_PERFIL", Decimal("1.15")),
        ("EDAD", "36", Decimal("0.95")),
        ("PLAZO", "180", Decimal("1.00")),
    ]
    assert desglose.prima_ajustada == Decimal(82593)
    assert desglose.recargos == ()
    assert (desglose.subtotal, desglose.ajuste_limite) == (Decimal(82593), Decimal(0))
    assert desglose.prima_total == Decimal(82593)


@pytest.mark.parametrize(
    ("nivel", "factor", "total"),
    [
        (NivelRiesgo.BAJO, "0.90", 64638),
        (NivelRiesgo.MEDIO, "1.00", 71820),
        (NivelRiesgo.ALTO, "1.30", 93366),
    ],
)
def test_factor_por_nivel_de_riesgo(
    reglas: Any, nivel: NivelRiesgo, factor: str, total: int
) -> None:
    desglose = calcular_prima(reglas, entradas(nivel_riesgo=nivel))
    assert desglose.factores[0].valor == nivel.value
    assert desglose.factores[0].factor == Decimal(factor)
    assert desglose.prima_total == Decimal(total)


def test_recargos_de_las_coberturas_opcionales(reglas: Any) -> None:
    """82.593 + R(82.593 × 0,04) + R(82.593 × 0,18) = 82.593 + 3.304 + 14.867."""
    desglose = calcular_prima(
        reglas, entradas(coberturas_opcionales=("ENFERMEDADES_GRAVES", "AUXILIO_FUNERARIO"))
    )
    assert [(r.cobertura, r.tasa, r.valor) for r in desglose.recargos] == [
        ("AUXILIO_FUNERARIO", Decimal("0.04"), Decimal(3304)),
        ("ENFERMEDADES_GRAVES", Decimal("0.18"), Decimal(14867)),
    ]
    assert desglose.subtotal == desglose.prima_total == Decimal(100764)


def test_el_desglose_cuadra(reglas: Any) -> None:
    desglose = calcular_prima(
        reglas, entradas(suma_asegurada=Decimal(10000000), edad=25, plazo_credito_meses=60)
    )
    recargos = sum((r.valor for r in desglose.recargos), Decimal(0))
    assert desglose.subtotal == desglose.prima_ajustada + recargos
    assert desglose.prima_total == desglose.subtotal + desglose.ajuste_limite


# --- Límites de la prima ------------------------------------------------------------------


def test_prima_minima(reglas: Any) -> None:
    """10.000.000 × 0,00042 = 4.200; × 0,90 × 0,80 × 0,95 = 2.873 → mínimo 15.000."""
    desglose = calcular_prima(
        reglas,
        entradas(
            suma_asegurada=Decimal(10000000),
            edad=25,
            plazo_credito_meses=60,
            nivel_riesgo=NivelRiesgo.BAJO,
        ),
    )
    assert desglose.prima_ajustada == Decimal(2873)
    assert desglose.prima_total == Decimal(15000)
    assert desglose.ajuste_limite == Decimal(12127)


def test_prima_maxima(reglas: Any) -> None:
    limitadas = replace(reglas, prima_maxima=Decimal(1000000))
    desglose = calcular_prima(
        limitadas,
        entradas(
            suma_asegurada=Decimal(1000000000),
            edad=65,
            plazo_credito_meses=360,
            nivel_riesgo=NivelRiesgo.ALTO,
        ),
    )
    assert desglose.prima_ajustada == Decimal(1297296)
    assert desglose.prima_total == Decimal(1000000)
    assert desglose.ajuste_limite == Decimal(-297296)


def test_prima_igual_al_minimo_no_ajusta(reglas: Any) -> None:
    exacta = replace(plano(reglas), prima_minima=Decimal(4200))
    desglose = calcular_prima(exacta, entradas(suma_asegurada=Decimal(10000000)))
    assert (desglose.prima_total, desglose.ajuste_limite) == (Decimal(4200), Decimal(0))


def test_valores_grandes_son_exactos(reglas: Any) -> None:
    """Un billón de pesos: Decimal no pierde dígitos (un float sí perdería el último)."""
    grande = plano(reglas, tasa_base=Decimal("0.000123456789"))
    desglose = calcular_prima(grande, entradas(suma_asegurada=Decimal(999999999999)))
    assert desglose.prima_base == Decimal(123456789)
    assert texto(desglose.prima_total) == "123456789"


# --- Redondeo -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tasa", "modo", "escala", "esperado"),
    [
        ("0.00000005", "ROUND_HALF_EVEN", 0, "0"),  # 0,5 → par
        ("0.00000015", "ROUND_HALF_EVEN", 0, "2"),  # 1,5 → par
        ("0.00000025", "ROUND_HALF_EVEN", 0, "2"),  # 2,5 → par
        ("0.00000005", "ROUND_HALF_UP", 0, "1"),
        ("0.00000005", "ROUND_DOWN", 0, "0"),
        ("0.00000001", "ROUND_UP", 0, "1"),
        ("0.0000000125", "ROUND_HALF_EVEN", 2, "0.12"),  # 0,125 → 0,12
        ("0.0000000135", "ROUND_HALF_EVEN", 2, "0.14"),  # 0,135 → 0,14
        ("0.0082593", "ROUND_HALF_EVEN", -2, "82600"),  # a centenas
    ],
)
def test_redondeo_por_modo_y_escala(
    reglas: Any, tasa: str, modo: str, escala: int, esperado: str
) -> None:
    unica = plano(reglas, tasa_base=Decimal(tasa), redondeo_modo=modo, redondeo_escala=escala)
    desglose = calcular_prima(unica, entradas(suma_asegurada=Decimal(10000000)))
    assert texto(desglose.prima_total) == esperado


# --- Validación de entradas: clases de equivalencia y límites ------------------------------


@pytest.mark.parametrize(
    ("cambio", "campo"),
    [
        ({"suma_asegurada": Decimal(0)}, CAMPO_SUMA),
        ({"suma_asegurada": Decimal(9999999)}, CAMPO_SUMA),
        ({"suma_asegurada": Decimal(1000000001)}, CAMPO_SUMA),
        ({"plazo_credito_meses": 11}, CAMPO_PLAZO),
        ({"plazo_credito_meses": 361}, CAMPO_PLAZO),
        ({"edad": 17}, CAMPO_NACIMIENTO),
        ({"edad": 71}, CAMPO_NACIMIENTO),
        ({"edad": -1}, CAMPO_NACIMIENTO),
    ],
    ids=[
        "suma 0",
        "suma mínimo-1",
        "suma máximo+1",
        "plazo mínimo-1",
        "plazo máximo+1",
        "edad mínima-1",
        "edad máxima+1",
        "nacimiento futuro",
    ],
)
def test_fuera_de_rango(producto: Any, reglas: Any, cambio: dict[str, Any], campo: str) -> None:
    with pytest.raises(DatosFueraDeRango) as error:
        validar_entradas(producto, reglas, entradas(**cambio))
    assert error.value.campos == (campo,)


@pytest.mark.parametrize(
    "cambio",
    [
        {"suma_asegurada": Decimal(10000000)},
        {"suma_asegurada": Decimal(1000000000)},
        {"plazo_credito_meses": 12},
        {"plazo_credito_meses": 360},
        {"edad": 18},
        {"edad": 70},
    ],
    ids=["suma mínima", "suma máxima", "plazo mínimo", "plazo máximo", "edad 18", "edad 70"],
)
def test_limites_incluidos(producto: Any, reglas: Any, cambio: dict[str, Any]) -> None:
    validar_entradas(producto, reglas, entradas(**cambio))


def test_varios_campos_fuera_de_rango_en_orden(producto: Any, reglas: Any) -> None:
    with pytest.raises(DatosFueraDeRango) as error:
        validar_entradas(producto, reglas, entradas(suma_asegurada=Decimal(1400000000), edad=11))
    assert error.value.campos == (CAMPO_SUMA, CAMPO_NACIMIENTO)


@pytest.mark.parametrize("cobertura", ["MUERTE", "DESCONOCIDA"])
def test_solo_coberturas_opcionales_del_producto(
    producto: Any, reglas: Any, cobertura: str
) -> None:
    with pytest.raises(CoberturaNoDisponible) as error:
        validar_entradas(producto, reglas, entradas(coberturas_opcionales=(cobertura,)))
    assert error.value.campos == (CAMPO_COBERTURAS,)


@pytest.mark.parametrize(
    ("nacimiento", "edad"),
    [
        (date(2008, 10, 15), 18),  # cumple 18 el día de la cotización
        (date(2008, 10, 16), 17),  # los cumple mañana
        (date(2008, 2, 29), 18),  # bisiesto: en 2026 se cumplen el 1 de marzo
        (date(1956, 10, 15), 70),
        (date(1955, 10, 15), 71),
    ],
)
def test_edad_cumplida_en_los_limites(nacimiento: date, edad: int) -> None:
    assert edad_cumplida(nacimiento, HOY) == edad


# --- AC3: determinismo --------------------------------------------------------------------


def test_misma_entrada_mismo_desglose_y_misma_huella(reglas: Any) -> None:
    entrada = entradas(
        nivel_riesgo=NivelRiesgo.MEDIO, coberturas_opcionales=("ENFERMEDADES_GRAVES",)
    )
    primero = calcular_prima(reglas, entrada)
    segundo = calcular_prima(reglas, entrada)
    assert primero == segundo
    assert huella_calculo(reglas, entrada, primero) == huella_calculo(reglas, entrada, segundo)


def test_no_depende_del_contexto_decimal_global(reglas: Any) -> None:
    esperado = calcular_prima(reglas, entradas())
    with decimal.localcontext(decimal.Context(prec=3, rounding=decimal.ROUND_FLOOR)):
        assert calcular_prima(reglas, entradas()) == esperado


def test_la_huella_cambia_con_la_version_o_el_hash_de_las_reglas(reglas: Any) -> None:
    entrada = entradas()
    desglose = calcular_prima(reglas, entrada)
    huella = huella_calculo(reglas, entrada, desglose)
    assert huella.startswith("sha256:")
    assert huella_calculo(replace(reglas, version="2026.09.2"), entrada, desglose) != huella
    assert huella_calculo(replace(reglas, hash="sha256:otro"), entrada, desglose) != huella


def test_formas_canonicas(reglas: Any) -> None:
    entrada = entradas(coberturas_opcionales=("ENFERMEDADES_GRAVES", "AUXILIO_FUNERARIO"))
    assert entradas_canonicas(entrada) == {
        "sumaAsegurada": "180000000",
        "plazoCreditoMeses": 180,
        "edad": 36,
        "nivelRiesgo": None,
        "coberturasOpcionales": ["AUXILIO_FUNERARIO", "ENFERMEDADES_GRAVES"],
    }
    canonico = desglose_canonico(calcular_prima(reglas, entrada))
    assert canonico["primaTotal"] == "100764"
    assert canonico["factores"][0] == {
        "codigo": "NIVEL_RIESGO",
        "valor": "SIN_PERFIL",
        "factor": "1.15",
    }


def test_un_factor_sin_tramo_abierto_es_un_error_de_contrato(reglas: Any) -> None:
    cerrado = FactorVariable("EDAD", "edad", (Tramo(Decimal(30), Decimal(1)),))
    with pytest.raises(ValueError, match="tramo abierto"):
        calcular_prima(replace(reglas, factores_variables=(cerrado,)), entradas())


def test_rango_contiene_sus_extremos() -> None:
    rango = Rango(Decimal(1), Decimal(2))
    assert rango.contiene(Decimal(1)) and rango.contiene(Decimal(2))
    assert not rango.contiene(Decimal("2.0001"))
    assert Limites(rango, rango, rango).edad is rango
