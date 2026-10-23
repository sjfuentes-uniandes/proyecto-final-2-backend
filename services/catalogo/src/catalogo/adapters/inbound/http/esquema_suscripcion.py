"""Esquema de la definición de las reglas de suscripción (HU-W13), validado en el borde.

Los decimales viajan como texto (DS2-16). La coherencia entre las partes (valor según el operador,
códigos y órdenes únicos, reservadas con NO_ELEGIBLE) la valida el dominio
(`domain/suscripcion/validacion.py`). El esquema JSON publicado se regenera con:
    uv run python -m catalogo.adapters.inbound.http.esquema_suscripcion \
> contracts/modelos/reglas-suscripcion.v1.schema.json
"""

import json
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from catalogo.adapters.inbound.http.modelos_riesgo import PATRON_PRODUCTO

TextoDecimal = Annotated[str, Field(pattern=r"^-?\d+(\.\d+)?$")]
ClaveTexto = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$", max_length=120)]


class TipoRegla(StrEnum):
    """EXCLUYENTE: si no se cumple, rechaza. REVISION: si no se cumple, envía a un suscriptor."""

    EXCLUYENTE = "EXCLUYENTE"
    REVISION = "REVISION"


class CampoRegla(StrEnum):
    """Conjunto cerrado de insumos de la oferta que una regla puede evaluar."""

    EDAD = "edad"
    SUMA_ASEGURADA = "suma_asegurada"
    PLAZO_CREDITO_MESES = "plazo_credito_meses"
    NIVEL_RIESGO = "nivel_riesgo"
    CONFIANZA_PERFIL = "confianza_perfil"
    COINCIDENCIAS_LISTAS_RESTRICTIVAS = "coincidencias_listas_restrictivas"


class OperadorRegla(StrEnum):
    MAYOR_O_IGUAL = ">="
    MENOR_O_IGUAL = "<="
    IGUAL = "=="
    DISTINTO = "!="
    ENTRE = "entre"
    EN = "en"


class Visibilidad(StrEnum):
    """RESERVADA: el valor del caso no se muestra al asesor ni al cliente (DS2-07)."""

    EXPLICABLE = "EXPLICABLE"
    RESERVADA = "RESERVADA"


class MotivoCliente(StrEnum):
    EDAD_FUERA_DE_RANGO = "EDAD_FUERA_DE_RANGO"
    PLAZO_FUERA_DE_RANGO = "PLAZO_FUERA_DE_RANGO"
    SUMA_REQUIERE_REVISION = "SUMA_REQUIERE_REVISION"
    PERFIL_REQUIERE_REVISION = "PERFIL_REQUIERE_REVISION"
    NO_ELEGIBLE = "NO_ELEGIBLE"


class CodigoDescriptor(StrEnum):
    EDAD_MAXIMA = "EDAD_MAXIMA"
    SIN_LIMITE_EDAD = "SIN_LIMITE_EDAD"


class _Estricto(BaseModel):
    model_config = ConfigDict(extra="forbid", alias_generator=to_camel)


class ReglaSuscripcion(_Estricto):
    """Una regla de la tabla de decisión. `valor` es texto (decimal o categoría) o, con `entre`
    y `en`, una lista de textos; su coherencia con el operador se valida en el dominio."""

    codigo: Annotated[str, Field(pattern=r"^SUS-\d{3}$")]
    orden: Annotated[int, Field(ge=1)]
    tipo: TipoRegla
    descripcion: ClaveTexto
    campo: CampoRegla
    operador: OperadorRegla
    valor: str | Annotated[list[str], Field(min_length=1, max_length=20)]
    visibilidad: Visibilidad
    motivo_cliente: MotivoCliente


class DescriptorAlternativa(_Estricto):
    """Lo que el portal muestra de la alternativa (E03): `EDAD_MAXIMA` con `valor` en años."""

    codigo: CodigoDescriptor
    valor: TextoDecimal | None


class Alternativa(_Estricto):
    """Producto alternativo, solo informativo (E03)."""

    producto_id: Annotated[str, Field(pattern=PATRON_PRODUCTO)]
    descriptor: DescriptorAlternativa


class DefinicionReglasSuscripcion(_Estricto):
    producto_id: Annotated[str, Field(pattern=PATRON_PRODUCTO)]
    alternativas: Annotated[list[Alternativa], Field(max_length=10)]
    reglas: Annotated[list[ReglaSuscripcion], Field(min_length=1, max_length=50)]


def a_dict(definicion: DefinicionReglasSuscripcion) -> dict[str, Any]:
    """La definición con las claves del contrato (camelCase), lista para el hash y la base."""
    return definicion.model_dump(mode="json", by_alias=True)


if __name__ == "__main__":
    print(json.dumps(DefinicionReglasSuscripcion.model_json_schema(), indent=2, sort_keys=True))
