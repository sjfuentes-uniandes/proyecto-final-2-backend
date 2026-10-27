"""Reglas de fallback versionadas (HU-W06).

Configuración, no código: cada versión dice, por fuente de datos del perfil, qué hace la cotización
si esa fuente no se obtuvo (tiempo agotado, caída o respuesta inválida). Una fuente sin regla
vigente no es degradable. La vigente es la de mayor vigente_desde ≤ fecha y, a diferencia de las
reglas de prima, puede vencer (`vigente_hasta`): un permiso para degradar puede ser temporal.
La huella es la de las demás reglas (`domain/reglas/hash.py`), sobre la definición con las claves
del contrato.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Conjuntos cerrados del contrato (contracts/modelos/reglas-fallback.v1.schema.json).
ACCIONES = frozenset({"DEGRADAR", "PENDIENTE"})
MENSAJES = frozenset(
    {
        "FUENTE_OPCIONAL_NO_DISPONIBLE",
        "FUENTE_REQUERIDA_NO_DISPONIBLE",
    }
)
# El ajuste es un factor sobre la prima ajustada: explícito en el desglose y acotado.
AJUSTE_MINIMO = "0.50"
AJUSTE_MAXIMO = "2.00"


@dataclass(frozen=True, slots=True)
class ReglasFallback:
    producto_id: str
    version: str
    vigente_desde: datetime
    # Vigencia semiabierta [vigente_desde, vigente_hasta); None = sin fin.
    vigente_hasta: datetime | None
    definicion: Mapping[str, Any]
    hash: str
    publicado_por: str
