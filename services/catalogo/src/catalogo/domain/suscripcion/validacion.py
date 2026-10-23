"""Validación semántica de las reglas de suscripción (HU-W13, W13-B10).

La forma (tipos, campos obligatorios, patrones) la garantiza el esquema del borde
(`esquema_suscripcion.DefinicionReglasSuscripcion`); aquí se validan los conjuntos cerrados y la
coherencia entre las partes, sin depender del borde. Los errores son códigos sin valores.
"""

import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from catalogo.domain.suscripcion.reglas import (
    CAMPOS,
    CAMPOS_CATEGORICOS,
    DESCRIPTORES,
    MOTIVOS,
    OPERADORES,
    TIPOS,
    VISIBILIDADES,
)

_DECIMAL = re.compile(r"-?\d+(\.\d+)?")
_ESCALARES = frozenset({">=", "<=", "==", "!="})


def validar_reglas_suscripcion(definicion: Mapping[str, Any], producto_id: str) -> tuple[str, ...]:
    """Códigos de las validaciones incumplidas; vacía si la definición es válida."""
    errores: list[str] = []
    if definicion["productoId"] != producto_id:
        errores.append("PRODUCTO_DISTINTO")
    reglas = definicion["reglas"]
    if not reglas:
        errores.append("SIN_REGLAS")
    codigos: set[str] = set()
    ordenes: set[int] = set()
    for regla in reglas:
        codigo = regla["codigo"]
        if codigo in codigos:
            errores.append(f"CODIGO_DUPLICADO:{codigo}")
        if regla["orden"] in ordenes:
            errores.append(f"ORDEN_DUPLICADO:{codigo}")
        codigos.add(codigo)
        ordenes.add(regla["orden"])
        errores.extend(_regla(regla))
    errores.extend(_alternativas(definicion["alternativas"]))
    return tuple(errores)


def _regla(regla: Mapping[str, Any]) -> list[str]:
    codigo = regla["codigo"]
    errores = [
        f"{nombre}:{codigo}"
        for nombre, valido in (
            ("TIPO_NO_SOPORTADO", regla["tipo"] in TIPOS),
            ("CAMPO_NO_SOPORTADO", regla["campo"] in CAMPOS),
            ("OPERADOR_NO_SOPORTADO", regla["operador"] in OPERADORES),
            ("VISIBILIDAD_NO_SOPORTADA", regla["visibilidad"] in VISIBILIDADES),
            ("MOTIVO_NO_SOPORTADO", regla["motivoCliente"] in MOTIVOS),
        )
        if not valido
    ]
    if not errores and not valor_coherente(regla["campo"], regla["operador"], regla["valor"]):
        errores.append(f"VALOR_INCOHERENTE:{codigo}")
    # DS2-07: una regla reservada nunca explica su causa al cliente.
    if regla["visibilidad"] == "RESERVADA" and regla["motivoCliente"] != "NO_ELEGIBLE":
        errores.append(f"RESERVADA_SIN_NO_ELEGIBLE:{codigo}")
    return errores


def valor_coherente(campo: str, operador: str, valor: Any) -> bool:
    """El valor tiene la forma que exige el operador y pertenece al dominio del campo."""
    if campo in CAMPOS_CATEGORICOS:
        categorias = CAMPOS_CATEGORICOS[campo]
        if operador in ("==", "!="):
            return isinstance(valor, str) and valor in categorias
        if operador == "en":
            return isinstance(valor, list) and bool(valor) and all(v in categorias for v in valor)
        return False
    if operador in _ESCALARES:
        return _es_decimal(valor)
    if operador == "entre":
        return (
            isinstance(valor, list)
            and len(valor) == 2
            and all(_es_decimal(v) for v in valor)
            and Decimal(valor[0]) <= Decimal(valor[1])
        )
    # "en"
    return isinstance(valor, list) and bool(valor) and all(_es_decimal(v) for v in valor)


def _es_decimal(valor: Any) -> bool:
    return isinstance(valor, str) and _DECIMAL.fullmatch(valor) is not None


def _alternativas(alternativas: list[Mapping[str, Any]]) -> list[str]:
    """Un producto alternativo una sola vez; EDAD_MAXIMA con un valor positivo y SIN_LIMITE_EDAD
    sin valor."""
    errores: list[str] = []
    vistas: set[str] = set()
    for alternativa in alternativas:
        producto = alternativa["productoId"]
        if producto in vistas:
            errores.append(f"ALTERNATIVA_DUPLICADA:{producto}")
        vistas.add(producto)
        descriptor = alternativa["descriptor"]
        codigo, valor = descriptor["codigo"], descriptor["valor"]
        coherente = codigo in DESCRIPTORES and (
            (codigo == "EDAD_MAXIMA" and _es_decimal(valor) and Decimal(valor) > 0)
            or (codigo == "SIN_LIMITE_EDAD" and valor is None)
        )
        if not coherente:
            errores.append(f"DESCRIPTOR_INCOHERENTE:{producto}")
    return errores
