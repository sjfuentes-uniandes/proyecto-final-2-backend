"""UT-W13-04: validación semántica de las reglas de suscripción y huella canónica (HU-W13)."""

import copy
import json
from importlib.resources import files
from typing import Any

import pytest
from catalogo.domain.reglas.hash import hash_canonico
from catalogo.domain.suscripcion.validacion import validar_reglas_suscripcion, valor_coherente

PRODUCTO = "vida-hipotecario"


@pytest.fixture
def definicion() -> dict[str, Any]:
    """La semilla 2026.10.1 (una copia nueva por prueba)."""
    semilla = files("catalogo.adapters.outbound") / "semillas" / "reglas_suscripcion_2026_10_1.json"
    definicion: dict[str, Any] = json.loads(semilla.read_text(encoding="utf-8"))
    return definicion


def _regla(definicion: dict[str, Any], codigo: str) -> dict[str, Any]:
    regla: dict[str, Any] = next(r for r in definicion["reglas"] if r["codigo"] == codigo)
    return regla


def test_la_semilla_es_valida(definicion: dict[str, Any]) -> None:
    assert validar_reglas_suscripcion(definicion, PRODUCTO) == ()
    assert [r["codigo"] for r in definicion["reglas"]] == [
        "SUS-001",
        "SUS-004",
        "SUS-007",
        "SUS-011",
        "SUS-015",
        "SUS-019",
    ]


def test_producto_distinto_y_sin_reglas(definicion: dict[str, Any]) -> None:
    definicion["reglas"] = []
    assert validar_reglas_suscripcion(definicion, "otro-producto") == (
        "PRODUCTO_DISTINTO",
        "SIN_REGLAS",
    )


def test_codigo_y_orden_duplicados(definicion: dict[str, Any]) -> None:
    definicion["reglas"].append(dict(definicion["reglas"][0]))
    errores = validar_reglas_suscripcion(definicion, PRODUCTO)
    assert errores == ("CODIGO_DUPLICADO:SUS-001", "ORDEN_DUPLICADO:SUS-001")


@pytest.mark.parametrize(
    ("clave", "valor", "error"),
    [
        ("tipo", "INFORMATIVA", "TIPO_NO_SOPORTADO"),
        ("campo", "ingreso_mensual_estimado", "CAMPO_NO_SOPORTADO"),
        ("operador", ">", "OPERADOR_NO_SOPORTADO"),
        ("visibilidad", "SECRETA", "VISIBILIDAD_NO_SOPORTADA"),
        ("motivoCliente", "OTRO", "MOTIVO_NO_SOPORTADO"),
    ],
)
def test_conjuntos_cerrados(definicion: dict[str, Any], clave: str, valor: str, error: str) -> None:
    _regla(definicion, "SUS-004")[clave] = valor
    assert validar_reglas_suscripcion(definicion, PRODUCTO) == (f"{error}:SUS-004",)


def test_valor_incoherente_con_el_operador(definicion: dict[str, Any]) -> None:
    _regla(definicion, "SUS-001")["valor"] = ["65", "18"]
    assert validar_reglas_suscripcion(definicion, PRODUCTO) == ("VALOR_INCOHERENTE:SUS-001",)


def test_una_reservada_solo_explica_no_elegible(definicion: dict[str, Any]) -> None:
    """DS2-07: SUS-015 nunca revela su causa al cliente."""
    _regla(definicion, "SUS-015")["motivoCliente"] = "PERFIL_REQUIERE_REVISION"
    assert validar_reglas_suscripcion(definicion, PRODUCTO) == (
        "RESERVADA_SIN_NO_ELEGIBLE:SUS-015",
    )


@pytest.mark.parametrize(
    ("campo", "operador", "valor", "esperado"),
    [
        # Numéricos con operadores escalares: un decimal en texto.
        ("suma_asegurada", "<=", "500000000", True),
        ("confianza_perfil", ">=", "0.70", True),
        ("edad", "==", "-1", True),
        ("coincidencias_listas_restrictivas", "!=", "0", True),
        ("suma_asegurada", "<=", "quinientos", False),
        ("suma_asegurada", "<=", "1e9", False),
        ("suma_asegurada", "<=", ["500000000"], False),
        # entre: exactamente dos decimales ordenados (los extremos pueden ser iguales).
        ("edad", "entre", ["18", "65"], True),
        ("edad", "entre", ["18", "18"], True),
        ("edad", "entre", ["65", "18"], False),
        ("edad", "entre", ["18"], False),
        ("edad", "entre", ["18", "65", "70"], False),
        ("edad", "entre", "18", False),
        ("edad", "entre", ["dieciocho", "65"], False),
        # en: una lista no vacía de decimales.
        ("plazo_credito_meses", "en", ["120", "240"], True),
        ("plazo_credito_meses", "en", [], False),
        ("plazo_credito_meses", "en", "120", False),
        ("plazo_credito_meses", "en", ["120", "x"], False),
        # Categórico: solo ==, != y en, con niveles conocidos.
        ("nivel_riesgo", "!=", "ALTO", True),
        ("nivel_riesgo", "==", "BAJO", True),
        ("nivel_riesgo", "en", ["BAJO", "MEDIO"], True),
        ("nivel_riesgo", "!=", "CRITICO", False),
        ("nivel_riesgo", "!=", ["ALTO"], False),
        ("nivel_riesgo", "en", [], False),
        ("nivel_riesgo", "en", ["BAJO", "CRITICO"], False),
        ("nivel_riesgo", ">=", "MEDIO", False),
        ("nivel_riesgo", "entre", ["BAJO", "ALTO"], False),
    ],
)
def test_tabla_de_coherencia_del_valor(
    campo: str, operador: str, valor: Any, esperado: bool
) -> None:
    assert valor_coherente(campo, operador, valor) is esperado


@pytest.mark.parametrize(
    ("descriptor", "valido"),
    [
        ({"codigo": "EDAD_MAXIMA", "valor": "80"}, True),
        ({"codigo": "SIN_LIMITE_EDAD", "valor": None}, True),
        ({"codigo": "EDAD_MAXIMA", "valor": None}, False),
        ({"codigo": "EDAD_MAXIMA", "valor": "0"}, False),
        ({"codigo": "EDAD_MAXIMA", "valor": "ochenta"}, False),
        ({"codigo": "SIN_LIMITE_EDAD", "valor": "80"}, False),
        ({"codigo": "OTRO", "valor": None}, False),
    ],
)
def test_descriptor_de_las_alternativas(
    definicion: dict[str, Any], descriptor: dict[str, Any], valido: bool
) -> None:
    definicion["alternativas"] = [{"productoId": "vida-senior", "descriptor": descriptor}]
    errores = validar_reglas_suscripcion(definicion, PRODUCTO)
    assert errores == (() if valido else ("DESCRIPTOR_INCOHERENTE:vida-senior",))


def test_alternativa_duplicada(definicion: dict[str, Any]) -> None:
    definicion["alternativas"].append(copy.deepcopy(definicion["alternativas"][0]))
    assert validar_reglas_suscripcion(definicion, PRODUCTO) == (
        "ALTERNATIVA_DUPLICADA:vida-senior",
    )


def test_huella_estable_y_sensible_a_las_reglas(definicion: dict[str, Any]) -> None:
    """La huella no depende del orden de las claves y cambia si cambia un umbral."""
    reordenada = {clave: definicion[clave] for clave in reversed(list(definicion))}
    reordenada["reglas"] = [dict(reversed(list(r.items()))) for r in definicion["reglas"]]
    assert hash_canonico(reordenada) == hash_canonico(definicion)
    assert hash_canonico(definicion).startswith("sha256:")
    otra = copy.deepcopy(definicion)
    _regla(otra, "SUS-004")["valor"] = "500000001"
    assert hash_canonico(otra) != hash_canonico(definicion)
