"""Validación semántica de las reglas de fallback (HU-W06).

La forma la garantiza el esquema del borde (`esquema_fallback.DefinicionReglasFallback`); aquí se
validan los conjuntos cerrados y la coherencia con el modelo de riesgo del producto:

- La obligatoriedad de una fuente sale del modelo (aporta un campo `obligatorio`); la regla la
  declara y debe coincidir, así no hay dos verdades.
- Una fuente obligatoria nunca se degrada: sin ella el motor no clasifica y no hay precio.
- El ajuste solo acompaña a DEGRADAR y está acotado.

Los errores son códigos sin valores.
"""

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from catalogo.domain.fallback.reglas import ACCIONES, AJUSTE_MAXIMO, AJUSTE_MINIMO, MENSAJES


def fuentes_obligatorias(modelo: Mapping[str, Any]) -> frozenset[str]:
    """Las fuentes del modelo de riesgo que aportan al menos un campo obligatorio."""
    obligatorios = set(modelo["obligatorios"])
    return frozenset(
        fuente
        for fuente, definicion in modelo["fuentes"].items()
        if obligatorios & set(definicion["campos"])
    )


def validar_reglas_fallback(
    definicion: Mapping[str, Any],
    producto_id: str,
    modelo: Mapping[str, Any],
    vigente_desde: datetime,
    vigente_hasta: datetime | None,
) -> tuple[str, ...]:
    """Códigos de las validaciones incumplidas; vacía si la definición es válida."""
    errores: list[str] = []
    if definicion["productoId"] != producto_id or modelo["productoId"] != producto_id:
        errores.append("PRODUCTO_DISTINTO")
    if vigente_hasta is not None and vigente_hasta <= vigente_desde:
        errores.append("VIGENCIA_INVALIDA")
    obligatorias = fuentes_obligatorias(modelo)
    ids: set[str] = set()
    fuentes: set[str] = set()
    for regla in definicion["reglas"]:
        id_, fuente = regla["id"], regla["fuente"]
        if id_ in ids:
            errores.append(f"ID_DUPLICADO:{id_}")
        if fuente in fuentes:
            errores.append(f"FUENTE_DUPLICADA:{id_}")
        ids.add(id_)
        fuentes.add(fuente)
        errores.extend(_regla(regla, modelo, obligatorias))
    return tuple(errores)


def _regla(
    regla: Mapping[str, Any], modelo: Mapping[str, Any], obligatorias: frozenset[str]
) -> list[str]:
    id_, fuente, accion, ajuste = regla["id"], regla["fuente"], regla["accion"], regla["ajuste"]
    errores = [
        f"{nombre}:{id_}"
        for nombre, valido in (
            ("FUENTE_FUERA_DEL_MODELO", fuente in modelo["fuentes"]),
            ("ACCION_NO_SOPORTADA", accion in ACCIONES),
            ("MENSAJE_NO_SOPORTADO", regla["mensajeCodigo"] in MENSAJES),
        )
        if not valido
    ]
    if fuente in modelo["fuentes"] and regla["obligatoria"] != (fuente in obligatorias):
        errores.append(f"OBLIGATORIEDAD_DISTINTA_DEL_MODELO:{id_}")
    if accion == "DEGRADAR" and (regla["obligatoria"] or fuente in obligatorias):
        errores.append(f"OBLIGATORIA_NO_DEGRADABLE:{id_}")
    if ajuste is not None:
        if accion != "DEGRADAR":
            errores.append(f"AJUSTE_SIN_DEGRADAR:{id_}")
        if not Decimal(AJUSTE_MINIMO) <= Decimal(ajuste) <= Decimal(AJUSTE_MAXIMO):
            errores.append(f"AJUSTE_FUERA_DE_RANGO:{id_}")
    return errores
