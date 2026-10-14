"""Esquema de la definición del modelo de riesgo (04 §B.6.1), validado en el borde.

Los decimales viajan como texto para no perder precisión. El esquema JSON publicado se regenera con:
    uv run python -m catalogo.adapters.inbound.http.esquema_modelo \
> contracts/modelos/modelo-riesgo.v1.schema.json
"""

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

TextoDecimal = Annotated[str, Field(pattern=r"^-?\d+(\.\d+)?$")]
Operador = Literal["==", "!=", "<", "<=", ">", ">="]
Condicion = tuple[str, Operador, TextoDecimal]
Direccion = Literal["REDUCE_RIESGO", "NEUTRO", "AUMENTA_LEVEMENTE", "AUMENTA_RIESGO"]
Nivel = Literal["BAJO", "MEDIO", "ALTO"]


class _Estricto(BaseModel):
    model_config = ConfigDict(extra="forbid", alias_generator=to_camel)


class DefFuente(_Estricto):
    tipo: Literal["FINANZAS_ABIERTAS", "DATOS_ABIERTOS"]
    campos: list[str]


class Expresion(_Estricto):
    op: Literal["add", "sub", "mul", "div"]
    args: list["str | Expresion"]


class Regla(_Estricto):
    si: list[Condicion]
    contribucion: TextoDecimal
    direccion: Direccion


class Factor(_Estricto):
    codigo: str
    fuente: str
    peso: TextoDecimal
    reglas: list[Regla]


class Ajuste(_Estricto):
    codigo: str
    si: list[Condicion]
    efecto: Literal["FORZAR_NIVEL"]
    nivel: Nivel


class UmbralNivel(_Estricto):
    desde: TextoDecimal
    nivel: Nivel


class Confianza(_Estricto):
    penalizacion_no_autorizada: TextoDecimal
    penalizacion_no_obtenida: TextoDecimal


class DefinicionModelo(_Estricto):
    producto_id: str
    proposito: str
    fuentes: dict[str, DefFuente]
    declarados: list[str]
    obligatorios: list[str]
    derivados: dict[str, Expresion]
    factores: list[Factor]
    ajustes: list[Ajuste]
    niveles: list[UmbralNivel]
    confianza: Confianza


def a_dict(definicion: DefinicionModelo) -> dict[str, Any]:
    """La definición con las claves del contrato (camelCase), lista para el hash y la base."""
    return definicion.model_dump(mode="json", by_alias=True)


if __name__ == "__main__":
    print(json.dumps(DefinicionModelo.model_json_schema(), indent=2, sort_keys=True))
