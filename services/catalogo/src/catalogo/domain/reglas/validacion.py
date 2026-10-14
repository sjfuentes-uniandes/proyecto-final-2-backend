"""Validación semántica de la definición del modelo de riesgo (04 §B.6.1).

La forma (tipos, campos y decimales como texto) la garantiza el esquema del borde
(`esquema_modelo.DefinicionModelo`); aquí se validan las reglas entre sus partes.
"""

import re
from collections.abc import Iterator, Mapping
from decimal import Decimal
from itertools import pairwise
from typing import Any

FUENTES_SOPORTADAS = frozenset(
    {
        "FA_PRODUCTOS_VIGENTES",
        "FA_HISTORIAL_PAGOS_12M",
        "FA_INGRESOS_AGREGADOS",
        "DA_REGISTROS_PUBLICOS",
    }
)
FUENTE_DECLARADA = "DECLARADO"

_LITERAL = re.compile(r"-?\d+(\.\d+)?")


def es_literal(arg: Any) -> bool:
    """En las expresiones, un texto numérico es un literal; cualquier otro, una variable."""
    return isinstance(arg, str) and _LITERAL.fullmatch(arg) is not None


def validar_definicion(definicion: Mapping[str, Any]) -> tuple[str, ...]:
    """Códigos de las reglas incumplidas, en el orden de la tabla de §B.6.1; vacía si es válida."""
    fuentes: Mapping[str, Any] = definicion["fuentes"]
    factores: list[Mapping[str, Any]] = definicion["factores"]
    derivados: Mapping[str, Any] = definicion["derivados"]
    base = {c for f in fuentes.values() for c in f["campos"]} | set(definicion["declarados"])
    errores: list[str] = []

    if sum((Decimal(f["peso"]) for f in factores), Decimal(0)) != 1:
        errores.append("PESOS_NO_SUMAN_1")
    for factor in factores:
        reglas = factor["reglas"]
        if not reglas or reglas[-1]["si"]:
            errores.append(f"FACTOR_SIN_REGLA_POR_DEFECTO:{factor['codigo']}")
    for factor in factores:
        if any(not 0 <= Decimal(r["contribucion"]) <= 1 for r in factor["reglas"]):
            errores.append(f"CONTRIBUCION_FUERA_DE_RANGO:{factor['codigo']}")
    if not _niveles_validos(definicion["niveles"]):
        errores.append("NIVELES_INVALIDOS")
    conocidas = base | set(derivados)
    for nombre in dict.fromkeys(_variables_referenciadas(definicion)):
        if nombre not in conocidas:
            errores.append(f"VARIABLE_DESCONOCIDA:{nombre}")
    for nombre in definicion["obligatorios"]:
        if nombre not in base:
            errores.append(f"OBLIGATORIO_DESCONOCIDO:{nombre}")
    for factor in factores:
        if factor["fuente"] != FUENTE_DECLARADA and factor["fuente"] not in fuentes:
            errores.append(f"FUENTE_DESCONOCIDA:{factor['codigo']}")
    for nombre, expresion in derivados.items():
        if _divide_por_cero(expresion) or _en_ciclo(nombre, derivados):
            errores.append(f"DERIVADO_INVALIDO:{nombre}")
    for clave in fuentes:
        if clave not in FUENTES_SOPORTADAS:
            errores.append(f"FUENTE_NO_SOPORTADA:{clave}")
    return tuple(errores)


def _niveles_validos(niveles: list[Mapping[str, Any]]) -> bool:
    desdes = [Decimal(n["desde"]) for n in niveles]
    descendente = all(a > b for a, b in pairwise(desdes))
    return bool(desdes) and descendente and desdes[-1] == 0


def _variables_referenciadas(definicion: Mapping[str, Any]) -> Iterator[str]:
    for factor in definicion["factores"]:
        for regla in factor["reglas"]:
            yield from (condicion[0] for condicion in regla["si"])
    for ajuste in definicion["ajustes"]:
        yield from (condicion[0] for condicion in ajuste["si"])
    for expresion in definicion["derivados"].values():
        yield from _variables_de(expresion)


def _variables_de(expresion: Mapping[str, Any]) -> Iterator[str]:
    for arg in expresion["args"]:
        if isinstance(arg, Mapping):
            yield from _variables_de(arg)
        elif not es_literal(arg):
            yield arg


def _divide_por_cero(expresion: Mapping[str, Any]) -> bool:
    args = expresion["args"]
    if expresion["op"] == "div" and any(es_literal(a) and Decimal(a) == 0 for a in args[1:]):
        return True
    return any(isinstance(a, Mapping) and _divide_por_cero(a) for a in args)


def _en_ciclo(nombre: str, derivados: Mapping[str, Any]) -> bool:
    """El derivado se alcanza a sí mismo a través de otros derivados."""
    pendientes = [v for v in _variables_de(derivados[nombre]) if v in derivados]
    vistos: set[str] = set()
    while pendientes:
        actual = pendientes.pop()
        if actual == nombre:
            return True
        if actual not in vistos:
            vistos.add(actual)
            pendientes.extend(v for v in _variables_de(derivados[actual]) if v in derivados)
    return False
