"""Validación semántica de las reglas de prima (HU-W03): cada regla rechaza su caso."""

import copy
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from catalogo.adapters.inbound.http.esquema_prima import DefinicionReglasPrima
from catalogo.adapters.outbound import productos_pg
from catalogo.adapters.outbound.productos_pg import semillas
from catalogo.domain.productos.errores import ReglasInvalidas
from catalogo.domain.productos.producto import Cobertura, EstadoProducto, Producto
from catalogo.domain.productos.validacion import validar_reglas
from pydantic import ValidationError


@pytest.fixture
def producto() -> Producto:
    return semillas()[0][0]


@pytest.fixture
def definicion() -> dict[str, Any]:
    return copy.deepcopy(semillas()[0][1][0][2])


def test_las_semillas_son_validas() -> None:
    publicadas = semillas()
    assert [p.id for p, _ in publicadas] == ["vida-hipotecario", "vida-hipotecario-2025"]
    for producto, reglas in publicadas:
        for _, _, definicion in reglas:
            assert validar_reglas(definicion, producto) == ()


CASOS: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
    ("producto", lambda d: d.update(productoId="otro"), "PRODUCTO_DISTINTO"),
    ("moneda", lambda d: d.update(moneda="USD"), "MONEDA_DISTINTA"),
    ("tasa 0", lambda d: d.update(tasaBase="0"), "TASA_BASE_NO_POSITIVA"),
    ("nivel 0", lambda d: d["factoresNivel"].update(ALTO="0"), "FACTOR_NO_POSITIVO:ALTO"),
    (
        "factor duplicado",
        lambda d: d["factoresVariables"].append(copy.deepcopy(d["factoresVariables"][0])),
        "FACTOR_DUPLICADO:EDAD",
    ),
    (
        "variable",
        lambda d: d["factoresVariables"][0].update(variable="ingresos"),
        "VARIABLE_NO_SOPORTADA:EDAD",
    ),
    (
        "último tramo cerrado",
        lambda d: d["factoresVariables"][0]["tramos"][-1].update(hasta="99"),
        "TRAMOS_INVALIDOS:EDAD",
    ),
    (
        "tramo abierto en medio",
        lambda d: d["factoresVariables"][0]["tramos"][0].update(hasta=None),
        "TRAMOS_INVALIDOS:EDAD",
    ),
    (
        "tramos no crecientes",
        lambda d: d["factoresVariables"][0]["tramos"][1].update(hasta="29"),
        "TRAMOS_INVALIDOS:EDAD",
    ),
    (
        "sin tramos",
        lambda d: d["factoresVariables"][1].update(tramos=[]),
        "TRAMOS_INVALIDOS:PLAZO",
    ),
    (
        "factor de tramo 0",
        lambda d: d["factoresVariables"][1]["tramos"][0].update(factor="0"),
        "FACTOR_NO_POSITIVO:PLAZO",
    ),
    (
        "recargo > 1",
        lambda d: d["recargos"][0].update(tasa="1.5"),
        "RECARGO_FUERA_DE_RANGO:ENFERMEDADES_GRAVES",
    ),
    (
        "recargo duplicado",
        lambda d: d["recargos"].append(dict(d["recargos"][0])),
        "RECARGO_DUPLICADO:ENFERMEDADES_GRAVES",
    ),
    (
        "recargo de obligatoria",
        lambda d: d["recargos"].append({"cobertura": "MUERTE", "tasa": "0.1"}),
        "RECARGO_SIN_COBERTURA_OPCIONAL:MUERTE",
    ),
    (
        "opcional sin recargo",
        lambda d: d.update(recargos=d["recargos"][:1]),
        "COBERTURA_OPCIONAL_SIN_RECARGO:AUXILIO_FUNERARIO",
    ),
    ("mínima > máxima", lambda d: d.update(primaMinima="9999999"), "PRIMA_MINIMA_MAXIMA_INVALIDA"),
    ("mínima negativa", lambda d: d.update(primaMinima="-1"), "PRIMA_MINIMA_MAXIMA_INVALIDA"),
    (
        "límite invertido",
        lambda d: d["limites"]["edad"].update(minimo="80"),
        "LIMITE_INVALIDO:edad",
    ),
    (
        "límite negativo",
        lambda d: d["limites"]["sumaAsegurada"].update(minimo="-1"),
        "LIMITE_INVALIDO:sumaAsegurada",
    ),
]


@pytest.mark.parametrize(("_caso", "cambio", "codigo"), CASOS, ids=[c[0] for c in CASOS])
def test_cada_regla_rechaza_su_caso(
    producto: Producto,
    definicion: dict[str, Any],
    _caso: str,
    cambio: Callable[[dict[str, Any]], None],
    codigo: str,
) -> None:
    cambio(definicion)
    assert codigo in validar_reglas(definicion, producto)


def test_el_esquema_exige_los_cuatro_niveles_y_un_modo_conocido(
    definicion: dict[str, Any],
) -> None:
    sin_nivel = copy.deepcopy(definicion)
    del sin_nivel["factoresNivel"]["SIN_PERFIL"]
    with pytest.raises(ValidationError):
        DefinicionReglasPrima.model_validate(sin_nivel)
    definicion["redondeo"]["modo"] = "ROUND_05UP"
    with pytest.raises(ValidationError):
        DefinicionReglasPrima.model_validate(definicion)


def test_una_semilla_invalida_impide_arrancar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(productos_pg, "validar_reglas", lambda d, p: ("TASA_BASE_NO_POSITIVA",))
    with pytest.raises(ReglasInvalidas) as error:
        semillas()
    assert error.value.errores == ("TASA_BASE_NO_POSITIVA",)


def test_opcionales_del_producto() -> None:
    producto = Producto(
        id="p",
        version="2026.09.1",
        nombre="P",
        ramo="R",
        pais="CO",
        moneda="COP",
        estado=EstadoProducto.ACTIVO,
        vigente_desde=datetime(2026, 9, 1, tzinfo=UTC),
        vigente_hasta=None,
        coberturas=(Cobertura("A", "A", True), Cobertura("B", "B", False)),
    )
    assert producto.opcionales == frozenset({"B"})
    assert replace(producto, coberturas=producto.coberturas[:1]).opcionales == frozenset()
