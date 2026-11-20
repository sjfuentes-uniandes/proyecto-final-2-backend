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
class Parametrico:
    """Regla de un producto paramétrico (HU-W21, W21-B04): paga `valor_pago` cuando un evento
    externo de `tipo_evento` alcanza `umbral` (en `unidad`). Decimales como texto (DS2-16).

    La zona y la ventana no son del producto: son de cada póliza (`coberturaParametrica.zona` y su
    vigencia en `PolizaEmitida`).
    """

    tipo_evento: str
    unidad: str
    umbral: str
    valor_pago: str
    version_regla: str


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
    # HU-W21: solo los productos paramétricos lo traen.
    parametrico: Parametrico | None = None

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
