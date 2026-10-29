"""UT-W06: validación de las reglas de fallback contra el modelo de riesgo (HU-W06)."""

import copy
import json
from datetime import UTC, datetime
from importlib.resources import files
from typing import Any

import pytest
from catalogo.adapters.outbound.modelos_pg import definicion_semilla
from catalogo.domain.fallback.validacion import fuentes_obligatorias, validar_reglas_fallback

PRODUCTO = "vida-hipotecario"
DESDE = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture
def definicion() -> dict[str, Any]:
    """La semilla 2026.10.1 (una copia nueva por prueba)."""
    semilla = files("catalogo.adapters.outbound") / "semillas" / "reglas_fallback_2026_10_1.json"
    definicion: dict[str, Any] = json.loads(semilla.read_text(encoding="utf-8"))
    return definicion


@pytest.fixture
def modelo() -> dict[str, Any]:
    return copy.deepcopy(definicion_semilla())


def _validar(definicion: dict[str, Any], modelo: dict[str, Any], **kwargs: Any) -> tuple[str, ...]:
    return validar_reglas_fallback(
        definicion,
        kwargs.get("producto", PRODUCTO),
        modelo,
        kwargs.get("desde", DESDE),
        kwargs.get("hasta"),
    )


def _regla(definicion: dict[str, Any], id_: str) -> dict[str, Any]:
    regla: dict[str, Any] = next(r for r in definicion["reglas"] if r["id"] == id_)
    return regla


def test_la_semilla_es_valida(definicion: dict[str, Any], modelo: dict[str, Any]) -> None:
    assert _validar(definicion, modelo) == ()
    acciones = {r["fuente"]: (r["obligatoria"], r["accion"]) for r in definicion["reglas"]}
    assert acciones == {
        "FA_PRODUCTOS_VIGENTES": (True, "PENDIENTE"),
        "FA_HISTORIAL_PAGOS_12M": (True, "PENDIENTE"),
        "FA_INGRESOS_AGREGADOS": (False, "DEGRADAR"),
        "DA_REGISTROS_PUBLICOS": (False, "DEGRADAR"),
    }


def test_la_obligatoriedad_sale_del_modelo(modelo: dict[str, Any]) -> None:
    """Obligatoria = aporta un campo obligatorio (moras_12m, cuota_mensual_obligaciones)."""
    assert fuentes_obligatorias(modelo) == {"FA_PRODUCTOS_VIGENTES", "FA_HISTORIAL_PAGOS_12M"}


def test_una_obligatoria_nunca_se_degrada(
    definicion: dict[str, Any], modelo: dict[str, Any]
) -> None:
    _regla(definicion, "FB-002")["accion"] = "DEGRADAR"
    assert _validar(definicion, modelo) == ("OBLIGATORIA_NO_DEGRADABLE:FB-002",)


def test_declararla_opcional_contradice_al_modelo(
    definicion: dict[str, Any], modelo: dict[str, Any]
) -> None:
    regla = _regla(definicion, "FB-002")
    regla["obligatoria"] = False
    regla["accion"] = "DEGRADAR"
    assert _validar(definicion, modelo) == (
        "OBLIGATORIEDAD_DISTINTA_DEL_MODELO:FB-002",
        "OBLIGATORIA_NO_DEGRADABLE:FB-002",
    )


def test_declarar_obligatoria_una_opcional(
    definicion: dict[str, Any], modelo: dict[str, Any]
) -> None:
    _regla(definicion, "FB-004").update(obligatoria=True, accion="PENDIENTE")
    assert _validar(definicion, modelo) == ("OBLIGATORIEDAD_DISTINTA_DEL_MODELO:FB-004",)


def test_fuente_fuera_del_modelo_y_conjuntos_cerrados(
    definicion: dict[str, Any], modelo: dict[str, Any]
) -> None:
    regla = _regla(definicion, "FB-003")
    regla.update(fuente="FA_OTRA", accion="RECHAZAR", mensajeCodigo="TEXTO_LIBRE")
    assert _validar(definicion, modelo) == (
        "FUENTE_FUERA_DEL_MODELO:FB-003",
        "ACCION_NO_SOPORTADA:FB-003",
        "MENSAJE_NO_SOPORTADO:FB-003",
    )


@pytest.mark.parametrize(
    ("id_", "ajuste", "errores"),
    [
        ("FB-003", "1.05", ()),
        ("FB-003", "0.50", ()),
        ("FB-003", "2.00", ()),
        ("FB-003", "0.49", ("AJUSTE_FUERA_DE_RANGO:FB-003",)),
        ("FB-003", "2.01", ("AJUSTE_FUERA_DE_RANGO:FB-003",)),
        ("FB-004", "1.05", ("AJUSTE_SIN_DEGRADAR:FB-004",)),
    ],
)
def test_el_ajuste_solo_con_degradar_y_acotado(
    definicion: dict[str, Any], modelo: dict[str, Any], id_: str, ajuste: str, errores: tuple
) -> None:
    _regla(definicion, id_)["ajuste"] = ajuste
    if id_ == "FB-004":
        _regla(definicion, id_)["accion"] = "PENDIENTE"
    assert _validar(definicion, modelo) == errores


def test_id_y_fuente_duplicados(definicion: dict[str, Any], modelo: dict[str, Any]) -> None:
    definicion["reglas"].append(dict(definicion["reglas"][0]))
    assert _validar(definicion, modelo) == ("ID_DUPLICADO:FB-001", "FUENTE_DUPLICADA:FB-001")


def test_producto_distinto_y_vigencia_invalida(
    definicion: dict[str, Any], modelo: dict[str, Any]
) -> None:
    assert _validar(definicion, modelo, producto="otro", hasta=DESDE) == (
        "PRODUCTO_DISTINTO",
        "VIGENCIA_INVALIDA",
    )
