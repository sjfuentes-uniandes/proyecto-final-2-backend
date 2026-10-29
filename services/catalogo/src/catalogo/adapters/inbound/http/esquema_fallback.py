"""Esquema de la definición de las reglas de fallback (HU-W06), validado en el borde.

Los decimales viajan como texto (DS2-16). La coherencia con el modelo de riesgo (fuente conocida,
obligatoriedad, una obligatoria nunca se degrada) y los rangos los valida el dominio
(`domain/fallback/validacion.py`). El esquema JSON publicado se regenera con:
    uv run python -m catalogo.adapters.inbound.http.esquema_fallback \
> contracts/modelos/reglas-fallback.v1.schema.json
"""

import json
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from catalogo.adapters.inbound.http.modelos_riesgo import PATRON_PRODUCTO

TextoDecimal = Annotated[str, Field(pattern=r"^\d+(\.\d+)?$")]


class AccionFallback(StrEnum):
    """DEGRADAR: oferta provisional sin el dato. PENDIENTE: sin oferta, estado recuperable."""

    DEGRADAR = "DEGRADAR"
    PENDIENTE = "PENDIENTE"


class MensajeFallback(StrEnum):
    """Clave del texto que muestran el portal y el socio; nunca un texto libre."""

    FUENTE_OPCIONAL_NO_DISPONIBLE = "FUENTE_OPCIONAL_NO_DISPONIBLE"
    FUENTE_REQUERIDA_NO_DISPONIBLE = "FUENTE_REQUERIDA_NO_DISPONIBLE"


class _Estricto(BaseModel):
    model_config = ConfigDict(extra="forbid", alias_generator=to_camel)


class ReglaFallback(_Estricto):
    """Qué hace la cotización si la fuente no se obtuvo. `obligatoria` debe coincidir con el
    modelo de riesgo; `ajuste` es un factor sobre la prima ajustada, solo con DEGRADAR."""

    id: Annotated[str, Field(pattern=r"^FB-\d{3}$")]
    fuente: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")]
    obligatoria: bool
    accion: AccionFallback
    ajuste: TextoDecimal | None
    mensaje_codigo: MensajeFallback


class DefinicionReglasFallback(_Estricto):
    producto_id: Annotated[str, Field(pattern=PATRON_PRODUCTO)]
    reglas: Annotated[list[ReglaFallback], Field(min_length=1, max_length=50)]


def a_dict(definicion: DefinicionReglasFallback) -> dict[str, Any]:
    """La definición con las claves del contrato (camelCase), lista para el hash y la base."""
    return definicion.model_dump(mode="json", by_alias=True)


if __name__ == "__main__":
    print(json.dumps(DefinicionReglasFallback.model_json_schema(), indent=2, sort_keys=True))
