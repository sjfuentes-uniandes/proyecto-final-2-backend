"""N1 HU-W03 AC2: tabla de decisión del producto y de las reglas (docs/hu-w03-prima.md §2) y
vigencia en los límites exactos de fecha."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cotizacion.domain.cotizacion.elegibilidad import (
    coherentes,
    verificar_elegibilidad,
    vigente_en,
)
from cotizacion.domain.cotizacion.errores import (
    ProductoFueraDeVigencia,
    ProductoInactivo,
    ProductoNoEncontrado,
    ReglasNoVigentes,
)
from cotizacion.domain.cotizacion.modelos import EstadoProducto

EN = datetime(2026, 10, 15, 15, 0, tzinfo=UTC)
DESDE = datetime(2026, 9, 1, tzinfo=UTC)
HASTA = datetime(2027, 1, 1, tzinfo=UTC)
UN_MICRO = timedelta(microseconds=1)


@pytest.fixture
def producto(semilla_w03: Any) -> Any:
    return semilla_w03[0](vigente_desde=DESDE, vigente_hasta=HASTA)


@pytest.fixture
def reglas(semilla_w03: Any) -> Any:
    return semilla_w03[1](vigente_desde=DESDE)


# Tabla de decisión: (fila, existe, estado, en vigencia, reglas vigentes) → causa.
TABLA = [
    ("1 inexistente", False, EstadoProducto.ACTIVO, True, True, ProductoNoEncontrado),
    ("1 inexistente sin reglas", False, EstadoProducto.ACTIVO, True, False, ProductoNoEncontrado),
    ("2 inactivo", True, EstadoProducto.INACTIVO, True, True, ProductoInactivo),
    ("2 inactivo y vencido", True, EstadoProducto.INACTIVO, False, False, ProductoInactivo),
    ("3 fuera de vigencia", True, EstadoProducto.ACTIVO, False, True, ProductoFueraDeVigencia),
    ("3 vencido sin reglas", True, EstadoProducto.ACTIVO, False, False, ProductoFueraDeVigencia),
    ("4 sin reglas vigentes", True, EstadoProducto.ACTIVO, True, False, ReglasNoVigentes),
    ("5 cotizable", True, EstadoProducto.ACTIVO, True, True, None),
]


@pytest.mark.parametrize(
    ("_fila", "existe", "estado", "en_vigencia", "con_reglas", "causa"),
    TABLA,
    ids=[f[0] for f in TABLA],
)
def test_tabla_de_decision(
    producto: Any,
    reglas: Any,
    _fila: str,
    existe: bool,
    estado: EstadoProducto,
    en_vigencia: bool,
    con_reglas: bool,
    causa: type[Exception] | None,
) -> None:
    candidato = replace(producto, estado=estado) if existe else None
    en = EN if en_vigencia else HASTA
    vigentes = reglas if con_reglas else None
    if causa is None:
        assert verificar_elegibilidad(candidato, vigentes, en) == (candidato, vigentes)
    else:
        with pytest.raises(causa):
            verificar_elegibilidad(candidato, vigentes, en)


@pytest.mark.parametrize(
    ("instante", "vigente"),
    [
        (DESDE - UN_MICRO, False),
        (DESDE, True),
        (HASTA - UN_MICRO, True),
        (HASTA, False),
    ],
    ids=["desde-1µs", "desde exacto", "hasta-1µs", "hasta exacto (excluido)"],
)
def test_vigencia_semiabierta_en_los_limites(
    producto: Any, instante: datetime, vigente: bool
) -> None:
    assert vigente_en(producto, instante) is vigente


def test_sin_fin_de_vigencia(producto: Any) -> None:
    assert vigente_en(replace(producto, vigente_hasta=None), datetime(2099, 1, 1, tzinfo=UTC))


def test_reglas_en_el_limite_exacto(producto: Any, reglas: Any) -> None:
    assert verificar_elegibilidad(producto, reglas, DESDE)[1] is reglas
    posteriores = replace(reglas, vigente_desde=EN + UN_MICRO)
    with pytest.raises(ReglasNoVigentes):
        verificar_elegibilidad(producto, posteriores, EN)


def test_coherencia_de_producto_y_reglas(producto: Any, reglas: Any) -> None:
    assert coherentes(producto, reglas)
    assert not coherentes(producto, replace(reglas, moneda="USD"))
    assert not coherentes(producto, replace(reglas, producto_id="otro"))
