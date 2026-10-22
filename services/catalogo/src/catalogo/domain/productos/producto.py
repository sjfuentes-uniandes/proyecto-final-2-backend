"""Producto de seguro y reglas de prima versionadas (HU-W03).

El producto se vende mientras está ACTIVO y la fecha cae en su vigencia semiabierta
[vigente_desde, vigente_hasta). Quien cotiza decide con esos datos (cotizacion, en la fecha de la
cotización); catalogo los entrega tal como están para que se puedan guardar en caché por id.

Las reglas de prima son inmutables: la vigente es la de mayor vigente_desde ≤ fecha (como los
modelos de riesgo, DT-03).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class EstadoProducto(StrEnum):
    ACTIVO = "ACTIVO"
    INACTIVO = "INACTIVO"


@dataclass(frozen=True, slots=True)
class Cobertura:
    codigo: str
    nombre: str
    obligatoria: bool


@dataclass(frozen=True, slots=True)
class Producto:
    id: str
    version: str
    nombre: str
    ramo: str
    pais: str
    moneda: str
    estado: EstadoProducto
    vigente_desde: datetime
    vigente_hasta: datetime | None
    coberturas: tuple[Cobertura, ...]

    @property
    def opcionales(self) -> frozenset[str]:
        return frozenset(c.codigo for c in self.coberturas if not c.obligatoria)


@dataclass(frozen=True, slots=True)
class ReglasPrima:
    producto_id: str
    version: str
    vigente_desde: datetime
    definicion: Mapping[str, Any]
    hash: str
    publicado_por: str
