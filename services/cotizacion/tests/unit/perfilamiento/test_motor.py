"""Motor de evaluación (UT-W11-01 a 12) con el modelo candidato 2026.10.1 y los casos de 01 §3.5."""

import random
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest
from cotizacion.adapters.outbound.catalogo_http import ModeloRiesgoDto, a_modelo_riesgo
from cotizacion.domain.perfilamiento.modelo_riesgo import (
    Condicion,
    DefAjuste,
    DefFactor,
    Expresion,
    ModeloRiesgo,
    ReglaFactor,
)
from cotizacion.domain.perfilamiento.modelos import (
    AccionSugerida,
    AjusteAplicado,
    DatoRequerido,
    Direccion,
    EstadoFactor,
    EstadoFuente,
    EstadoPerfil,
    Fuente,
    MotivoDato,
    NivelRiesgo,
    ResultadoEvaluacion,
)
from cotizacion.domain.perfilamiento.motor import FuenteEvaluada, evaluar

P, H, I, D = (
    Fuente.FA_PRODUCTOS_VIGENTES,
    Fuente.FA_HISTORIAL_PAGOS_12M,
    Fuente.FA_INGRESOS_AGREGADOS,
    Fuente.DA_REGISTROS_PUBLICOS,
)
CAMPOS = {
    P: ("antiguedad_productos_anios", "cuota_mensual_obligaciones", "entidades_con_deuda"),
    H: ("moras_12m",),
    I: ("ingreso_mensual_estimado",),
    D: ("coincidencias_listas_restrictivas",),
}
Variables = dict[str, Decimal]
Fuentes = dict[Fuente, FuenteEvaluada]


def _sin(variables: Variables, fuentes: Fuentes, fuente: Fuente, estado: EstadoFuente) -> None:
    """La fuente queda en `estado` y sus campos no llegan al motor."""
    for campo in CAMPOS[fuente]:
        variables.pop(campo, None)
    fuentes[fuente] = FuenteEvaluada(estado, None)


def _factor(resultado: ResultadoEvaluacion, codigo: str) -> Any:
    return next(f for f in resultado.factores if f.codigo == codigo)


def _req(campo: str, fuente: Fuente | None, motivo: MotivoDato) -> DatoRequerido:
    return DatoRequerido(campo, fuente, motivo)


# --- UT-W11-01 · oráculos ---------------------------------------------------------------


def c02(v: Variables, f: Fuentes) -> None:
    _sin(v, f, I, EstadoFuente.NO_AUTORIZADA)


def c06(v: Variables, f: Fuentes) -> None:
    _sin(v, f, D, EstadoFuente.TIEMPO_AGOTADO)


def c07(v: Variables, f: Fuentes) -> None:
    _sin(v, f, I, EstadoFuente.NO_DISPONIBLE)


def c14(v: Variables, f: Fuentes) -> None:
    v["coincidencias_listas_restrictivas"] = Decimal(1)


def c15(v: Variables, f: Fuentes) -> None:
    v["moras_12m"] = Decimal(1)


@pytest.mark.parametrize(
    ("caso", "estado", "nivel", "puntaje", "confianza", "accion"),
    [
        (lambda v, f: None, EstadoPerfil.CALCULADO, NivelRiesgo.BAJO, "75.90", "0.92", None),
        (c02, EstadoPerfil.CALCULADO, NivelRiesgo.BAJO, "75.00", "0.87", None),
        (c06, EstadoPerfil.DEGRADADO, NivelRiesgo.BAJO, "74.09", "0.79", AccionSugerida.RECALCULAR),
        (c07, EstadoPerfil.DEGRADADO, NivelRiesgo.BAJO, "75.00", "0.78", AccionSugerida.RECALCULAR),
        (c14, EstadoPerfil.CALCULADO, NivelRiesgo.ALTO, "68.90", "0.92", None),
        (c15, EstadoPerfil.CALCULADO, NivelRiesgo.MEDIO, "66.90", "0.92", None),
    ],
    ids=["C01", "C02", "C06", "C07", "C14", "C15"],
)
def test_ut_w11_01_oraculos(
    modelo: ModeloRiesgo,
    variables_c01: Variables,
    fuentes_c01: Fuentes,
    caso: Callable[[Variables, Fuentes], None],
    estado: EstadoPerfil,
    nivel: NivelRiesgo,
    puntaje: str,
    confianza: str,
    accion: AccionSugerida | None,
) -> None:
    caso(variables_c01, fuentes_c01)
    resultado = evaluar(modelo, variables_c01, fuentes_c01)
    assert (resultado.estado, resultado.nivel, resultado.accion) == (estado, nivel, accion)
    assert resultado.puntaje == Decimal(puntaje)
    assert str(resultado.puntaje) == puntaje and str(resultado.confianza) == confianza
    assert [f.codigo for f in resultado.factores] == [
        "PUNTUALIDAD_PAGOS",
        "ANTIGUEDAD_PRODUCTOS",
        "RELACION_CUOTA_INGRESO",
        "EDAD_PLAZO",
        "CONCENTRACION_DEUDA",
        "LISTAS_RESTRICTIVAS",
    ]
    assert resultado.datos_requeridos == ()


def test_c01_factores_y_direcciones(
    modelo: ModeloRiesgo, variables_c01: Variables, fuentes_c01: Fuentes
) -> None:
    resultado = evaluar(modelo, variables_c01, fuentes_c01)
    assert [(f.contribucion, f.direccion) for f in resultado.factores] == [
        (Decimal("0.90"), Direccion.REDUCE_RIESGO),
        (Decimal("0.80"), Direccion.REDUCE_RIESGO),
        (Decimal("0.80"), Direccion.REDUCE_RIESGO),
        (Decimal("0.50"), Direccion.NEUTRO),
        (Decimal("0.40"), Direccion.AUMENTA_LEVEMENTE),
        (Decimal("1.00"), Direccion.REDUCE_RIESGO),
    ]
    assert all(f.estado is EstadoFactor.EVALUADO and f.motivo is None for f in resultado.factores)
    assert resultado.ajustes == ()


def test_c14_registra_el_ajuste(
    modelo: ModeloRiesgo, variables_c01: Variables, fuentes_c01: Fuentes
) -> None:
    c14(variables_c01, fuentes_c01)
    resultado = evaluar(modelo, variables_c01, fuentes_c01)
    assert resultado.ajustes == (
        AjusteAplicado("COINCIDENCIA_LISTAS_RESTRICTIVAS", "FORZAR_NIVEL", NivelRiesgo.ALTO),
    )


# --- Casos insuficientes de 01 §3.5 (AC3) -----------------------------------------------


def c03(v: Variables, f: Fuentes) -> None:
    v.pop("moras_12m")


def c05(v: Variables, f: Fuentes) -> None:
    _sin(v, f, P, EstadoFuente.RESPUESTA_INVALIDA)


def c08(v: Variables, f: Fuentes) -> None:
    for fuente in Fuente:
        _sin(v, f, fuente, EstadoFuente.NO_AUTORIZADA)


def c13(v: Variables, f: Fuentes) -> None:
    _sin(v, f, H, EstadoFuente.TIEMPO_AGOTADO)


def c16(v: Variables, f: Fuentes) -> None:
    _sin(v, f, H, EstadoFuente.NO_AUTORIZADA)
    for fuente in (P, I, D):
        _sin(v, f, fuente, EstadoFuente.NO_CONSULTADA)


def c17(v: Variables, f: Fuentes) -> None:
    for fuente in (P, H, I):
        _sin(v, f, fuente, EstadoFuente.NO_AUTORIZADA)
    _sin(v, f, D, EstadoFuente.NO_CONSULTADA)


NO_AUT = MotivoDato.NO_AUTORIZADO
COMPLETAR = AccionSugerida.COMPLETAR_CONSENTIMIENTO


@pytest.mark.parametrize(
    ("caso", "requeridos", "accion", "confianza"),
    [
        (c03, [_req("moras_12m", H, MotivoDato.NO_DISPONIBLE_EN_FUENTE)], None, "0.92"),
        (
            c05,
            [_req("cuota_mensual_obligaciones", P, MotivoDato.RESPUESTA_INVALIDA)],
            AccionSugerida.RECALCULAR,
            "0.77",
        ),
        (
            c08,
            [_req("cuota_mensual_obligaciones", P, NO_AUT), _req("moras_12m", H, NO_AUT)],
            COMPLETAR,
            "0.00",
        ),
        (
            c13,
            [_req("moras_12m", H, MotivoDato.TIEMPO_AGOTADO)],
            AccionSugerida.RECALCULAR,
            "0.78",
        ),
        (c16, [_req("moras_12m", H, NO_AUT)], COMPLETAR, "0.00"),
        (
            c17,
            [_req("cuota_mensual_obligaciones", P, NO_AUT), _req("moras_12m", H, NO_AUT)],
            COMPLETAR,
            "0.00",
        ),
    ],
    ids=["C03", "C05", "C08", "C13", "C16", "C17"],
)
def test_casos_insuficientes(
    modelo: ModeloRiesgo,
    variables_c01: Variables,
    fuentes_c01: Fuentes,
    caso: Callable[[Variables, Fuentes], None],
    requeridos: list[DatoRequerido],
    accion: AccionSugerida | None,
    confianza: str,
) -> None:
    caso(variables_c01, fuentes_c01)
    resultado = evaluar(modelo, variables_c01, fuentes_c01)
    assert resultado.estado is EstadoPerfil.INFORMACION_INSUFICIENTE
    assert (resultado.nivel, resultado.puntaje, resultado.factores, resultado.ajustes) == (
        None,
        None,
        (),
        (),
    )
    assert list(resultado.datos_requeridos) == requeridos
    assert resultado.accion == accion
    assert str(resultado.confianza) == confianza


# --- UT-W11-02 · límites de los umbrales ------------------------------------------------


def _contribucion(
    modelo: ModeloRiesgo, variables: Variables, fuentes: Fuentes, codigo: str, **valores: str
) -> tuple[Decimal | None, Direccion | None]:
    variables.update({k: Decimal(v) for k, v in valores.items()})
    factor = _factor(evaluar(modelo, variables, fuentes), codigo)
    return factor.contribucion, factor.direccion


@pytest.mark.parametrize(
    ("codigo", "valores", "contribucion", "direccion"),
    [
        ("PUNTUALIDAD_PAGOS", {"moras_12m": "0"}, "0.90", Direccion.REDUCE_RIESGO),
        ("PUNTUALIDAD_PAGOS", {"moras_12m": "1"}, "0.60", Direccion.NEUTRO),
        ("PUNTUALIDAD_PAGOS", {"moras_12m": "2"}, "0.35", Direccion.AUMENTA_LEVEMENTE),
        ("PUNTUALIDAD_PAGOS", {"moras_12m": "3"}, "0.10", Direccion.AUMENTA_RIESGO),
        (
            "ANTIGUEDAD_PRODUCTOS",
            {"antiguedad_productos_anios": "1.99"},
            "0.40",
            Direccion.AUMENTA_LEVEMENTE,
        ),
        ("ANTIGUEDAD_PRODUCTOS", {"antiguedad_productos_anios": "2"}, "0.60", Direccion.NEUTRO),
        ("ANTIGUEDAD_PRODUCTOS", {"antiguedad_productos_anios": "4.99"}, "0.60", Direccion.NEUTRO),
        (
            "ANTIGUEDAD_PRODUCTOS",
            {"antiguedad_productos_anios": "5"},
            "0.80",
            Direccion.REDUCE_RIESGO,
        ),
        *[
            (
                "RELACION_CUOTA_INGRESO",
                {"cuota_mensual_obligaciones": cuota, "ingreso_mensual_estimado": "1000000"},
                contribucion,
                direccion,
            )
            for cuota, contribucion, direccion in [
                ("300000", "0.80", Direccion.REDUCE_RIESGO),
                ("300100", "0.55", Direccion.NEUTRO),
                ("400000", "0.55", Direccion.NEUTRO),
                ("500000", "0.35", Direccion.AUMENTA_LEVEMENTE),
                ("500100", "0.15", Direccion.AUMENTA_RIESGO),
            ]
        ],
        *[
            ("EDAD_PLAZO", {"edad": edad, "plazo_credito_meses": "180"}, contribucion, direccion)
            for edad, contribucion, direccion in [
                ("30", "0.80", Direccion.REDUCE_RIESGO),
                ("30.01", "0.50", Direccion.NEUTRO),
                ("45", "0.50", Direccion.NEUTRO),
                ("55", "0.30", Direccion.AUMENTA_LEVEMENTE),
                ("55.01", "0.10", Direccion.AUMENTA_RIESGO),
            ]
        ],
        ("CONCENTRACION_DEUDA", {"entidades_con_deuda": "1"}, "0.80", Direccion.REDUCE_RIESGO),
        ("CONCENTRACION_DEUDA", {"entidades_con_deuda": "2"}, "0.40", Direccion.AUMENTA_LEVEMENTE),
        ("CONCENTRACION_DEUDA", {"entidades_con_deuda": "3"}, "0.40", Direccion.AUMENTA_LEVEMENTE),
        ("CONCENTRACION_DEUDA", {"entidades_con_deuda": "4"}, "0.15", Direccion.AUMENTA_RIESGO),
        (
            "LISTAS_RESTRICTIVAS",
            {"coincidencias_listas_restrictivas": "0"},
            "1.00",
            Direccion.REDUCE_RIESGO,
        ),
        (
            "LISTAS_RESTRICTIVAS",
            {"coincidencias_listas_restrictivas": "1"},
            "0.00",
            Direccion.AUMENTA_RIESGO,
        ),
    ],
)
def test_ut_w11_02_limites_de_umbrales(
    modelo: ModeloRiesgo,
    variables_c01: Variables,
    fuentes_c01: Fuentes,
    codigo: str,
    valores: dict[str, str],
    contribucion: str,
    direccion: Direccion,
) -> None:
    obtenido = _contribucion(modelo, variables_c01, fuentes_c01, codigo, **valores)
    assert obtenido == (Decimal(contribucion), direccion)


# --- UT-W11-03 · niveles ----------------------------------------------------------------


def _un_factor(modelo: ModeloRiesgo, contribucion: str, **cambios: Any) -> ModeloRiesgo:
    """Un solo factor de peso 1: el puntaje es contribucion × 100."""
    factor = DefFactor(
        "UNICO",
        "DECLARADO",
        Decimal(1),
        (ReglaFactor((), Decimal(contribucion), Direccion.NEUTRO),),
    )
    return replace(modelo, factores=(factor,), **cambios)


@pytest.mark.parametrize(
    ("contribucion", "nivel"),
    [
        ("0.70", NivelRiesgo.BAJO),
        ("0.6999", NivelRiesgo.MEDIO),
        ("0.50", NivelRiesgo.MEDIO),
        ("0.4999", NivelRiesgo.ALTO),
        ("0", NivelRiesgo.ALTO),
    ],
)
def test_ut_w11_03_niveles(
    modelo: ModeloRiesgo,
    variables_c01: Variables,
    fuentes_c01: Fuentes,
    contribucion: str,
    nivel: NivelRiesgo,
) -> None:
    resultado = evaluar(_un_factor(modelo, contribucion), variables_c01, fuentes_c01)
    assert resultado.nivel is nivel
    assert resultado.puntaje == (Decimal(contribucion) * 100).quantize(Decimal("0.01"))


def test_puntaje_redondea_a_la_par(
    modelo: ModeloRiesgo, variables_c01: Variables, fuentes_c01: Fuentes
) -> None:
    assert evaluar(_un_factor(modelo, "0.69995"), variables_c01, fuentes_c01).puntaje == Decimal(
        "70.00"
    )
    assert evaluar(_un_factor(modelo, "0.69985"), variables_c01, fuentes_c01).puntaje == Decimal(
        "69.98"
    )


# --- UT-W11-04 · falta cada obligatorio con cada motivo (AC3) ----------------------------


@pytest.mark.parametrize(
    ("estado", "motivo"),
    [
        (EstadoFuente.CONSULTADA, MotivoDato.NO_DISPONIBLE_EN_FUENTE),
        (EstadoFuente.NO_AUTORIZADA, MotivoDato.NO_AUTORIZADO),
        (EstadoFuente.TIEMPO_AGOTADO, MotivoDato.TIEMPO_AGOTADO),
        (EstadoFuente.NO_DISPONIBLE, MotivoDato.FUENTE_NO_DISPONIBLE),
        (EstadoFuente.RESPUESTA_INVALIDA, MotivoDato.RESPUESTA_INVALIDA),
    ],
)
@pytest.mark.parametrize(("fuente", "campo"), [(H, "moras_12m"), (P, "cuota_mensual_obligaciones")])
def test_ut_w11_04_obligatorio_faltante_con_cada_motivo(
    modelo: ModeloRiesgo,
    variables_c01: Variables,
    fuentes_c01: Fuentes,
    fuente: Fuente,
    campo: str,
    estado: EstadoFuente,
    motivo: MotivoDato,
) -> None:
    variables_c01.pop(campo)
    confianza = fuentes_c01[fuente].confianza if estado is EstadoFuente.CONSULTADA else None
    fuentes_c01[fuente] = FuenteEvaluada(estado, confianza)
    resultado = evaluar(modelo, variables_c01, fuentes_c01)
    assert resultado.estado is EstadoPerfil.INFORMACION_INSUFICIENTE
    assert resultado.datos_requeridos == (DatoRequerido(campo, fuente, motivo),)
    assert resultado.nivel is None and resultado.puntaje is None


def test_ut_w11_04_orden_de_los_requeridos(
    modelo: ModeloRiesgo, variables_c01: Variables, fuentes_c01: Fuentes
) -> None:
    """Orden de aparición en el modelo: declarados y luego los campos de cada fuente."""
    for campo in ("moras_12m", "cuota_mensual_obligaciones", "plazo_credito_meses", "edad"):
        variables_c01.pop(campo)
    resultado = evaluar(modelo, variables_c01, fuentes_c01)
    assert [r.campo for r in resultado.datos_requeridos] == [
        "edad",
        "plazo_credito_meses",
        "cuota_mensual_obligaciones",
        "moras_12m",
    ]
    declarado = resultado.datos_requeridos[0]
    assert (declarado.fuente, declarado.motivo) == (None, MotivoDato.NO_DISPONIBLE_EN_FUENTE)


def test_obligatorio_fuera_de_las_variables_del_modelo_va_al_final(
    modelo: ModeloRiesgo, variables_c01: Variables, fuentes_c01: Fuentes
) -> None:
    modelo = replace(modelo, obligatorios=modelo.obligatorios | {"zz_dato", "aa_dato"})
    variables_c01.pop("moras_12m")
    resultado = evaluar(modelo, variables_c01, fuentes_c01)
    assert [r.campo for r in resultado.datos_requeridos] == ["moras_12m", "aa_dato", "zz_dato"]


# --- UT-W11-05 y 06 · NO_CONSULTADA y redistribución -----------------------------------


def test_ut_w11_05_no_consultadas_no_son_requeridas(
    modelo: ModeloRiesgo, variables_c01: Variables, fuentes_c01: Fuentes
) -> None:
    c16(variables_c01, fuentes_c01)
    requeridos = evaluar(modelo, variables_c01, fuentes_c01).datos_requeridos
    assert [r.fuente for r in requeridos] == [H]


def test_ut_w11_06_no_evaluado_redistribuye_pesos(
    modelo: ModeloRiesgo, variables_c01: Variables, fuentes_c01: Fuentes
) -> None:
    c02(variables_c01, fuentes_c01)
    resultado = evaluar(modelo, variables_c01, fuentes_c01)
    relacion = _factor(resultado, "RELACION_CUOTA_INGRESO")
    assert relacion.estado is EstadoFactor.NO_EVALUADO
    assert (relacion.contribucion, relacion.direccion, relacion.motivo) == (None, None, NO_AUT)
    assert relacion.peso == Decimal("0.18")
    # 0,615 / 0,82 = 0,75: los demás factores pesan sobre el total evaluado.
    assert resultado.puntaje == Decimal("75.00")


# --- UT-W11-07 · ajustes ----------------------------------------------------------------


def _ajuste(nivel: NivelRiesgo) -> DefAjuste:
    condicion = Condicion("coincidencias_listas_restrictivas", ">=", Decimal(1))
    return DefAjuste("FORZAR", (condicion,), "FORZAR_NIVEL", nivel)


@pytest.mark.parametrize(
    ("contribucion", "forzado", "esperado"),
    [
        ("0.40", NivelRiesgo.MEDIO, NivelRiesgo.ALTO),
        ("0.60", NivelRiesgo.BAJO, NivelRiesgo.MEDIO),
        ("0.80", NivelRiesgo.MEDIO, NivelRiesgo.MEDIO),
        ("0.80", NivelRiesgo.ALTO, NivelRiesgo.ALTO),
    ],
)
def test_ut_w11_07_forzar_nivel_solo_sube(
    modelo: ModeloRiesgo,
    variables_c01: Variables,
    fuentes_c01: Fuentes,
    contribucion: str,
    forzado: NivelRiesgo,
    esperado: NivelRiesgo,
) -> None:
    variables_c01["coincidencias_listas_restrictivas"] = Decimal(1)
    ajustado = _un_factor(modelo, contribucion, ajustes=(_ajuste(forzado),))
    resultado = evaluar(ajustado, variables_c01, fuentes_c01)
    assert resultado.nivel is esperado
    assert [a.nivel for a in resultado.ajustes] == [forzado]


def test_ajuste_sin_su_variable_no_se_aplica(
    modelo: ModeloRiesgo, variables_c01: Variables, fuentes_c01: Fuentes
) -> None:
    c06(variables_c01, fuentes_c01)
    assert evaluar(modelo, variables_c01, fuentes_c01).ajustes == ()


# --- UT-W11-08 · confianza --------------------------------------------------------------


def test_ut_w11_08_confianza_sin_fuentes_consultadas(
    modelo: ModeloRiesgo, variables_c01: Variables, fuentes_c01: Fuentes
) -> None:
    c08(variables_c01, fuentes_c01)
    assert evaluar(modelo, variables_c01, fuentes_c01).confianza == Decimal("0.00")


def test_ut_w11_08_confianza_consultada_sin_valor_no_cuenta(
    modelo: ModeloRiesgo, variables_c01: Variables, fuentes_c01: Fuentes
) -> None:
    fuentes_c01[D] = FuenteEvaluada(EstadoFuente.CONSULTADA, None)
    # Promedio de 0,95, 0,91 y 0,93 sin penalización.
    assert evaluar(modelo, variables_c01, fuentes_c01).confianza == Decimal("0.93")


@pytest.mark.parametrize(
    ("penalizaciones", "esperado"),
    [
        ({"penalizacion_no_obtenida": Decimal("0.99")}, "0.00"),
        ({"penalizacion_no_autorizada": Decimal(-1)}, "1.00"),
    ],
    ids=["piso-0", "techo-1"],
)
def test_ut_w11_08_confianza_se_limita_entre_0_y_1(
    modelo: ModeloRiesgo,
    variables_c01: Variables,
    fuentes_c01: Fuentes,
    penalizaciones: dict[str, Decimal],
    esperado: str,
) -> None:
    _sin(variables_c01, fuentes_c01, I, EstadoFuente.NO_DISPONIBLE)
    _sin(variables_c01, fuentes_c01, D, EstadoFuente.NO_AUTORIZADA)
    resultado = evaluar(replace(modelo, **penalizaciones), variables_c01, fuentes_c01)
    assert str(resultado.confianza) == esperado


# --- UT-W11-09 y 10 · determinismo y versiones (AC2, AC4) --------------------------------


def test_ut_w11_09_determinismo_con_permutaciones(
    modelo: ModeloRiesgo, variables_c01: Variables, fuentes_c01: Fuentes
) -> None:
    c02(variables_c01, fuentes_c01)
    esperado = evaluar(modelo, variables_c01, fuentes_c01)
    azar = random.Random(20261015)
    for _ in range(50):
        variables = list(variables_c01.items())
        fuentes = list(fuentes_c01.items())
        azar.shuffle(variables)
        azar.shuffle(fuentes)
        assert evaluar(modelo, dict(variables), dict(fuentes)) == esperado


def test_ut_w11_10_versiones_distintas_dan_niveles_distintos(
    modelo: ModeloRiesgo,
    respuesta_candidata: dict[str, Any],
    variables_c01: Variables,
    fuentes_c01: Fuentes,
) -> None:
    respuesta_candidata["version"] = "2026.10.2"
    respuesta_candidata["definicion"]["niveles"][0]["desde"] = "80"
    nueva = a_modelo_riesgo(ModeloRiesgoDto.model_validate(respuesta_candidata))
    assert evaluar(modelo, variables_c01, fuentes_c01).nivel is NivelRiesgo.BAJO
    assert evaluar(nueva, variables_c01, fuentes_c01).nivel is NivelRiesgo.MEDIO


# --- UT-W11-11 y 12 · división por cero y W = 0 -----------------------------------------


def test_ut_w11_11_division_por_cero(
    modelo: ModeloRiesgo, variables_c01: Variables, fuentes_c01: Fuentes
) -> None:
    variables_c01["ingreso_mensual_estimado"] = Decimal(0)
    resultado = evaluar(modelo, variables_c01, fuentes_c01)
    relacion = _factor(resultado, "RELACION_CUOTA_INGRESO")
    assert (relacion.estado, relacion.motivo) == (
        EstadoFactor.NO_EVALUADO,
        MotivoDato.NO_DISPONIBLE_EN_FUENTE,
    )
    assert (resultado.estado, resultado.puntaje) == (EstadoPerfil.CALCULADO, Decimal("75.00"))


def test_ut_w11_12_sin_factores_evaluados_es_insuficiente(
    modelo: ModeloRiesgo, variables_c01: Variables, fuentes_c01: Fuentes
) -> None:
    sin_declarados = replace(
        modelo,
        obligatorios=frozenset({"edad", "plazo_credito_meses"}),
        factores=tuple(f for f in modelo.factores if f.fuente != "DECLARADO"),
    )
    for fuente in Fuente:
        _sin(variables_c01, fuentes_c01, fuente, EstadoFuente.NO_DISPONIBLE)
    resultado = evaluar(sin_declarados, variables_c01, fuentes_c01)
    assert resultado.estado is EstadoPerfil.INFORMACION_INSUFICIENTE
    assert resultado.factores == () and resultado.nivel is None
    assert [(r.campo, r.fuente) for r in resultado.datos_requeridos] == [
        ("moras_12m", H),
        ("antiguedad_productos_anios", P),
        ("cuota_mensual_obligaciones", P),
        ("entidades_con_deuda", P),
        ("coincidencias_listas_restrictivas", D),
    ]
    assert {r.motivo for r in resultado.datos_requeridos} == {MotivoDato.FUENTE_NO_DISPONIBLE}
    assert resultado.accion is AccionSugerida.RECALCULAR


# --- Expresiones y modelo mal formado ---------------------------------------------------


@pytest.mark.parametrize(
    ("expresion", "regla_que_aplica"),
    [
        (Expresion("sub", ("edad", Decimal(1), Decimal(5))), "30"),
        (Expresion("mul", ("edad", Decimal("0.5"))), "18"),
        (Expresion("add", ("edad", Expresion("div", ("plazo_credito_meses", Decimal(0))))), None),
        (Expresion("add", ("edad", "sin_dato")), None),
    ],
    ids=["sub", "mul", "div-anidada-por-cero", "variable-faltante"],
)
def test_derivados_con_cada_operacion(
    modelo: ModeloRiesgo,
    variables_c01: Variables,
    fuentes_c01: Fuentes,
    expresion: Expresion,
    regla_que_aplica: str | None,
) -> None:
    reglas = (
        ReglaFactor((Condicion("x", "==", Decimal(30)),), Decimal("0.30"), Direccion.NEUTRO),
        ReglaFactor((Condicion("x", "==", Decimal(18)),), Decimal("0.18"), Direccion.NEUTRO),
        ReglaFactor((), Decimal(0), Direccion.AUMENTA_RIESGO),
    )
    factor = DefFactor("X", "DECLARADO", Decimal(1), reglas)
    con_x = replace(modelo, derivados={"x": expresion}, factores=(factor,), ajustes=())
    resultado = evaluar(con_x, variables_c01, fuentes_c01)
    if regla_que_aplica is None:
        assert resultado.estado is EstadoPerfil.INFORMACION_INSUFICIENTE
    else:
        assert resultado.factores[0].contribucion == Decimal(regla_que_aplica) / 100


def test_factor_sin_regla_por_defecto_es_un_error_del_modelo(
    modelo: ModeloRiesgo, variables_c01: Variables, fuentes_c01: Fuentes
) -> None:
    regla = ReglaFactor((Condicion("edad", "<", Decimal(0)),), Decimal(1), Direccion.NEUTRO)
    roto = replace(modelo, factores=(DefFactor("ROTO", "DECLARADO", Decimal(1), (regla,)),))
    with pytest.raises(ValueError, match="ROTO"):
        evaluar(roto, variables_c01, fuentes_c01)


@pytest.mark.parametrize("operador", ["!=", "<", ">"])
def test_operadores_de_comparacion(
    modelo: ModeloRiesgo, variables_c01: Variables, fuentes_c01: Fuentes, operador: str
) -> None:
    regla = ReglaFactor((Condicion("edad", operador, Decimal(36)),), Decimal(1), Direccion.NEUTRO)
    defecto = ReglaFactor((), Decimal(0), Direccion.AUMENTA_RIESGO)
    factor = DefFactor("OP", "DECLARADO", Decimal(1), (regla, defecto))
    resultado = evaluar(replace(modelo, factores=(factor,)), variables_c01, fuentes_c01)
    assert resultado.factores[0].contribucion == Decimal(0)
