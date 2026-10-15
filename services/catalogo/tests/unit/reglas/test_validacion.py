from collections.abc import Callable
from typing import Any

import pytest
from catalogo.domain.reglas.validacion import es_literal, validar_definicion

Definicion = dict[str, Any]


def _factor(d: Definicion, codigo: str) -> Definicion:
    return next(f for f in d["factores"] if f["codigo"] == codigo)


def test_el_modelo_candidato_es_valido(definicion_candidata: Definicion) -> None:
    assert validar_definicion(definicion_candidata) == ()


def _pesos(d: Definicion) -> None:
    _factor(d, "PUNTUALIDAD_PAGOS")["peso"] = "0.31"


def _sin_regla_por_defecto(d: Definicion) -> None:
    _factor(d, "ANTIGUEDAD_PRODUCTOS")["reglas"].pop()


def _sin_reglas(d: Definicion) -> None:
    _factor(d, "ANTIGUEDAD_PRODUCTOS")["reglas"] = []


def _contribucion_mayor(d: Definicion) -> None:
    _factor(d, "EDAD_PLAZO")["reglas"][0]["contribucion"] = "1.01"


def _contribucion_negativa(d: Definicion) -> None:
    _factor(d, "EDAD_PLAZO")["reglas"][-1]["contribucion"] = "-0.01"


def _niveles_desordenados(d: Definicion) -> None:
    d["niveles"][0]["desde"], d["niveles"][1]["desde"] = "50", "70"


def _niveles_repetidos(d: Definicion) -> None:
    d["niveles"][1]["desde"] = "70"


def _ultimo_nivel_no_cero(d: Definicion) -> None:
    d["niveles"][-1]["desde"] = "10"


def _sin_niveles(d: Definicion) -> None:
    d["niveles"] = []


def _variable_en_factor(d: Definicion) -> None:
    _factor(d, "PUNTUALIDAD_PAGOS")["reglas"][0]["si"][0][0] = "moras_24m"


def _variable_en_ajuste(d: Definicion) -> None:
    d["ajustes"][0]["si"][0][0] = "coincidencias"


def _variable_en_derivado(d: Definicion) -> None:
    d["derivados"]["edad_fin_credito"]["args"][1]["args"][0] = "plazo"


def _obligatorio(d: Definicion) -> None:
    d["obligatorios"].append("relacion_cuota_ingreso")


def _fuente_de_factor(d: Definicion) -> None:
    _factor(d, "CONCENTRACION_DEUDA")["fuente"] = "FA_DESCONOCIDA"


def _division_por_cero(d: Definicion) -> None:
    d["derivados"]["edad_fin_credito"]["args"][1]["args"][1] = "0.0"


def _ciclo(d: Definicion) -> None:
    d["derivados"]["a"] = {"op": "add", "args": ["b", "1"]}
    d["derivados"]["b"] = {"op": "mul", "args": [{"op": "sub", "args": ["a", "edad"]}, "2"]}


def _autorreferencia(d: Definicion) -> None:
    d["derivados"]["edad_fin_credito"]["args"][0] = "edad_fin_credito"


def _fuente_no_soportada(d: Definicion) -> None:
    d["fuentes"]["FA_SALDOS"] = {"tipo": "FINANZAS_ABIERTAS", "campos": ["saldo_promedio"]}


@pytest.mark.parametrize(
    ("alterar", "esperado"),
    [
        (_pesos, ("PESOS_NO_SUMAN_1",)),
        (_sin_regla_por_defecto, ("FACTOR_SIN_REGLA_POR_DEFECTO:ANTIGUEDAD_PRODUCTOS",)),
        (_contribucion_mayor, ("CONTRIBUCION_FUERA_DE_RANGO:EDAD_PLAZO",)),
        (_contribucion_negativa, ("CONTRIBUCION_FUERA_DE_RANGO:EDAD_PLAZO",)),
        (_niveles_desordenados, ("NIVELES_INVALIDOS",)),
        (_niveles_repetidos, ("NIVELES_INVALIDOS",)),
        (_ultimo_nivel_no_cero, ("NIVELES_INVALIDOS",)),
        (_sin_niveles, ("NIVELES_INVALIDOS",)),
        (_variable_en_factor, ("VARIABLE_DESCONOCIDA:moras_24m",)),
        (_variable_en_ajuste, ("VARIABLE_DESCONOCIDA:coincidencias",)),
        (_variable_en_derivado, ("VARIABLE_DESCONOCIDA:plazo",)),
        (_obligatorio, ("OBLIGATORIO_DESCONOCIDO:relacion_cuota_ingreso",)),
        (_fuente_de_factor, ("FUENTE_DESCONOCIDA:CONCENTRACION_DEUDA",)),
        (_division_por_cero, ("DERIVADO_INVALIDO:edad_fin_credito",)),
        (_ciclo, ("DERIVADO_INVALIDO:a", "DERIVADO_INVALIDO:b")),
        (_autorreferencia, ("DERIVADO_INVALIDO:edad_fin_credito",)),
        (_fuente_no_soportada, ("FUENTE_NO_SOPORTADA:FA_SALDOS",)),
    ],
)
def test_cada_regla_incumplida_tiene_su_codigo(
    definicion_candidata: Definicion,
    alterar: Callable[[Definicion], None],
    esperado: tuple[str, ...],
) -> None:
    alterar(definicion_candidata)
    assert validar_definicion(definicion_candidata) == esperado


def test_factor_sin_reglas_no_tiene_regla_por_defecto(definicion_candidata: Definicion) -> None:
    _sin_reglas(definicion_candidata)
    assert validar_definicion(definicion_candidata) == (
        "FACTOR_SIN_REGLA_POR_DEFECTO:ANTIGUEDAD_PRODUCTOS",
    )


def test_varios_incumplimientos_salen_en_el_orden_de_la_tabla(
    definicion_candidata: Definicion,
) -> None:
    _fuente_no_soportada(definicion_candidata)
    _variable_en_factor(definicion_candidata)
    _pesos(definicion_candidata)
    assert validar_definicion(definicion_candidata) == (
        "PESOS_NO_SUMAN_1",
        "VARIABLE_DESCONOCIDA:moras_24m",
        "FUENTE_NO_SOPORTADA:FA_SALDOS",
    )


def test_una_variable_desconocida_se_informa_una_sola_vez(
    definicion_candidata: Definicion,
) -> None:
    for regla in _factor(definicion_candidata, "PUNTUALIDAD_PAGOS")["reglas"][:3]:
        regla["si"][0][0] = "moras_24m"
    assert validar_definicion(definicion_candidata) == ("VARIABLE_DESCONOCIDA:moras_24m",)


def test_dividir_por_una_variable_no_es_division_por_cero(
    definicion_candidata: Definicion,
) -> None:
    definicion_candidata["derivados"]["relacion_cuota_ingreso"]["args"] = ["0", "edad"]
    assert validar_definicion(definicion_candidata) == ()


@pytest.mark.parametrize(
    ("arg", "literal"),
    [("12", True), ("0.30", True), ("-1", True), ("1.", False), ("edad", False), ("1e3", False)],
)
def test_literal_numerico(arg: str, literal: bool) -> None:
    assert es_literal(arg) is literal


def test_derivados_que_comparten_dependencias_sin_ciclo_son_validos(
    definicion_candidata: Definicion,
) -> None:
    definicion_candidata["derivados"]["d"] = {"op": "add", "args": ["edad", "1"]}
    definicion_candidata["derivados"]["c"] = {"op": "mul", "args": ["d", "2"]}
    definicion_candidata["derivados"]["x"] = {"op": "sub", "args": ["c", "d"]}
    assert validar_definicion(definicion_candidata) == ()
