"""Reglas de suscripción versionadas (HU-W13, DS2-02).

Configuración, no código: cada versión es una tabla de decisión inmutable que `polizas` evalúa
sobre los insumos de la oferta. La vigente es la de mayor vigente_desde ≤ fecha, como las reglas de
prima y los modelos de riesgo. La huella es la misma que la de esas reglas
(`domain/reglas/hash.py`), sobre la definición con las claves del contrato.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Conjuntos cerrados del contrato (contracts/modelos/reglas-suscripcion.v1.schema.json).
TIPOS = frozenset({"EXCLUYENTE", "REVISION"})
CAMPOS_NUMERICOS = frozenset(
    {
        "edad",
        "suma_asegurada",
        "plazo_credito_meses",
        "confianza_perfil",
        "coincidencias_listas_restrictivas",
    }
)
CAMPOS_CATEGORICOS: Mapping[str, frozenset[str]] = {
    "nivel_riesgo": frozenset({"BAJO", "MEDIO", "ALTO"}),
}
CAMPOS = CAMPOS_NUMERICOS | frozenset(CAMPOS_CATEGORICOS)
OPERADORES = frozenset({">=", "<=", "==", "!=", "entre", "en"})
VISIBILIDADES = frozenset({"EXPLICABLE", "RESERVADA"})
MOTIVOS = frozenset(
    {
        "EDAD_FUERA_DE_RANGO",
        "PLAZO_FUERA_DE_RANGO",
        "SUMA_REQUIERE_REVISION",
        "PERFIL_REQUIERE_REVISION",
        "NO_ELEGIBLE",
    }
)
DESCRIPTORES = frozenset({"EDAD_MAXIMA", "SIN_LIMITE_EDAD"})


@dataclass(frozen=True, slots=True)
class ReglasSuscripcion:
    producto_id: str
    version: str
    vigente_desde: datetime
    definicion: Mapping[str, Any]
    hash: str
    publicado_por: str
