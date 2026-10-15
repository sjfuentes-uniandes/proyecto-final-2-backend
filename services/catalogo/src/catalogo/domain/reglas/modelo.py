"""Modelo de riesgo publicado: una versión inmutable de la definición (DT-03)."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class ModeloPublicado:
    version: str
    producto_id: str
    vigente_desde: datetime
    definicion: Mapping[str, Any]
    hash: str
    publicado_por: str
