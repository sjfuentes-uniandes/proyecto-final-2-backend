"""Validación semántica de las reglas de prima contra su producto (HU-W03).

La forma (tipos, campos, decimales como texto, modo y escala del redondeo) la garantiza el esquema
del borde (`esquema_prima.DefinicionReglasPrima`); aquí se validan las reglas entre sus partes.
"""

from collections.abc import Mapping
from decimal import Decimal
from itertools import pairwise
from typing import Any

from catalogo.domain.productos.producto import Producto

# Variables declaradas con las que una regla puede elegir un tramo.
VARIABLES_SOPORTADAS = frozenset({"edad", "plazo_credito_meses", "suma_asegurada"})


def validar_reglas(definicion: Mapping[str, Any], producto: Producto) -> tuple[str, ...]:
    """Códigos de las reglas incumplidas, sin valores; vacía si la definición es válida."""
    errores: list[str] = []
    if definicion["productoId"] != producto.id:
        errores.append("PRODUCTO_DISTINTO")
    if definicion["moneda"] != producto.moneda:
        errores.append("MONEDA_DISTINTA")
    if Decimal(definicion["tasaBase"]) <= 0:
        errores.append("TASA_BASE_NO_POSITIVA")
    for nivel, factor in definicion["factoresNivel"].items():
        if Decimal(factor) <= 0:
            errores.append(f"FACTOR_NO_POSITIVO:{nivel}")
    errores.extend(_factores_variables(definicion["factoresVariables"]))
    errores.extend(_recargos(definicion["recargos"], producto))
    minima, maxima = Decimal(definicion["primaMinima"]), Decimal(definicion["primaMaxima"])
    if minima < 0 or minima > maxima:
        errores.append("PRIMA_MINIMA_MAXIMA_INVALIDA")
    for campo, rango in definicion["limites"].items():
        if Decimal(rango["minimo"]) < 0 or Decimal(rango["minimo"]) > Decimal(rango["maximo"]):
            errores.append(f"LIMITE_INVALIDO:{campo}")
    if definicion.get("oferta") is not None:
        errores.extend(_oferta(definicion["oferta"], producto))
    return tuple(errores)


def _factores_variables(factores: list[Mapping[str, Any]]) -> list[str]:
    errores: list[str] = []
    vistos: set[str] = set()
    for factor in factores:
        codigo = factor["codigo"]
        if codigo in vistos:
            errores.append(f"FACTOR_DUPLICADO:{codigo}")
        vistos.add(codigo)
        if factor["variable"] not in VARIABLES_SOPORTADAS:
            errores.append(f"VARIABLE_NO_SOPORTADA:{codigo}")
        if not _tramos_validos(factor["tramos"]):
            errores.append(f"TRAMOS_INVALIDOS:{codigo}")
        if any(Decimal(t["factor"]) <= 0 for t in factor["tramos"]):
            errores.append(f"FACTOR_NO_POSITIVO:{codigo}")
    return errores


def _tramos_validos(tramos: list[Mapping[str, Any]]) -> bool:
    """Límites superiores estrictamente crecientes y un último tramo abierto (`hasta` nulo)."""
    if not tramos or tramos[-1]["hasta"] is not None:
        return False
    cerrados = [t["hasta"] for t in tramos[:-1]]
    if any(h is None for h in cerrados):
        return False
    return all(Decimal(a) < Decimal(b) for a, b in pairwise(cerrados))


def _recargos(recargos: list[Mapping[str, Any]], producto: Producto) -> list[str]:
    """Un recargo por cobertura opcional del producto, con tasa entre 0 y 1."""
    errores: list[str] = []
    con_recargo: set[str] = set()
    for recargo in recargos:
        cobertura = recargo["cobertura"]
        if cobertura in con_recargo:
            errores.append(f"RECARGO_DUPLICADO:{cobertura}")
        con_recargo.add(cobertura)
        if cobertura not in producto.opcionales:
            errores.append(f"RECARGO_SIN_COBERTURA_OPCIONAL:{cobertura}")
        if not 0 <= Decimal(recargo["tasa"]) <= 1:
            errores.append(f"RECARGO_FUERA_DE_RANGO:{cobertura}")
    for cobertura in sorted(producto.opcionales - con_recargo):
        errores.append(f"COBERTURA_OPCIONAL_SIN_RECARGO:{cobertura}")
    return errores


def _oferta(oferta: Mapping[str, Any], producto: Producto) -> list[str]:
    """HU-W04: exclusiones sin códigos repetidos y una suma por cobertura del producto, en (0, 1]."""
    errores: list[str] = []
    codigos = [e["codigo"] for e in oferta["exclusiones"]]
    for codigo in sorted({c for c in codigos if codigos.count(c) > 1}):
        errores.append(f"EXCLUSION_DUPLICADA:{codigo}")
    del_producto = {c.codigo for c in producto.coberturas}
    vistas: set[str] = set()
    for suma in oferta["sumasCoberturas"]:
        cobertura = suma["cobertura"]
        if cobertura in vistas:
            errores.append(f"SUMA_DUPLICADA:{cobertura}")
        vistas.add(cobertura)
        if cobertura not in del_producto:
            errores.append(f"SUMA_SIN_COBERTURA:{cobertura}")
        if not 0 < Decimal(suma["porcentaje"]) <= 1:
            errores.append(f"SUMA_FUERA_DE_RANGO:{cobertura}")
    return errores
