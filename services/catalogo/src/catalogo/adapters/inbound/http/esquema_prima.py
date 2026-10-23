"""Esquema de la definición de las reglas de prima (HU-W03), validado en el borde.

Los decimales viajan como texto para no perder precisión. El esquema JSON publicado se regenera con:
    uv run python -m catalogo.adapters.inbound.http.esquema_prima \
> contracts/modelos/reglas-prima.v1.schema.json
"""

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

TextoDecimal = Annotated[str, Field(pattern=r"^-?\d+(\.\d+)?$")]
ModoRedondeo = Literal[
    "ROUND_HALF_EVEN", "ROUND_HALF_UP", "ROUND_HALF_DOWN", "ROUND_UP", "ROUND_DOWN"
]


class _Estricto(BaseModel):
    model_config = ConfigDict(extra="forbid", alias_generator=to_camel)


class FactoresNivel(BaseModel):
    """Factor por nivel de riesgo del perfil; SIN_PERFIL se aplica cuando no hay nivel."""

    # Sin alias: las claves son los niveles tal cual.
    model_config = ConfigDict(extra="forbid")
    BAJO: TextoDecimal
    MEDIO: TextoDecimal
    ALTO: TextoDecimal
    SIN_PERFIL: TextoDecimal


class Tramo(_Estricto):
    """Factor para los valores ≤ `hasta`; el último tramo es abierto (`hasta` nulo)."""

    hasta: TextoDecimal | None
    factor: TextoDecimal


class FactorVariable(_Estricto):
    codigo: str
    variable: str
    tramos: list[Tramo]


class Recargo(_Estricto):
    """Recargo de una cobertura opcional: `tasa` × prima ajustada."""

    cobertura: str
    tasa: TextoDecimal


class Redondeo(_Estricto):
    escala: Annotated[int, Field(ge=-6, le=6)]
    modo: ModoRedondeo


class Rango(_Estricto):
    minimo: TextoDecimal
    maximo: TextoDecimal


class Limites(_Estricto):
    suma_asegurada: Rango
    plazo_credito_meses: Rango
    edad: Rango


class SumaCobertura(_Estricto):
    """Parte de la suma asegurada que cubre una cobertura (0 < porcentaje ≤ 1); sin entrada, 1."""

    cobertura: str
    porcentaje: TextoDecimal


class Exclusion(_Estricto):
    """Exclusión resumida que el socio muestra al cliente; el texto legal está en el condicionado."""

    codigo: Annotated[str, Field(pattern=r"^[A-Z0-9_]{1,64}$")]
    descripcion: Annotated[str, Field(min_length=1, max_length=200)]


class CondicionesOferta(_Estricto):
    """Condiciones de la oferta que se crea con la prima (HU-W04).

    `vigenciaMinutos` cuenta desde el cálculo: vigente si ahora < calculadoEn + vigenciaMinutos.
    """

    vigencia_minutos: Annotated[int, Field(ge=1, le=525_600)]
    exclusiones: Annotated[list[Exclusion], Field(max_length=20)]
    sumas_coberturas: list[SumaCobertura]


class DefinicionReglasPrima(_Estricto):
    producto_id: str
    moneda: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    periodicidad: Literal["MENSUAL", "ANUAL"]
    tasa_base: TextoDecimal
    factores_nivel: FactoresNivel
    factores_variables: list[FactorVariable]
    recargos: list[Recargo]
    prima_minima: TextoDecimal
    prima_maxima: TextoDecimal
    redondeo: Redondeo
    limites: Limites
    # HU-W04: opcional y aditivo. Sin él la clave no se serializa (ni en la API, ni en la caché, ni
    # en la base): el hash de las reglas publicadas antes (2026.09.1) se sigue verificando igual.
    oferta: CondicionesOferta | None = Field(default=None, exclude_if=lambda v: v is None)


def a_dict(definicion: DefinicionReglasPrima) -> dict[str, Any]:
    """La definición con las claves del contrato (camelCase), lista para el hash y la base."""
    return definicion.model_dump(mode="json", by_alias=True)


if __name__ == "__main__":
    print(json.dumps(DefinicionReglasPrima.model_json_schema(), indent=2, sort_keys=True))
