"""Contrato v1 del evento externo (HU-W21, DS3-16) y su validación.

`EventoExterno` es el cuerpo que firma y envía la fuente. Es también el esquema que publica el
OpenAPI (`contracts/openapi/adaptador-telemetria.yaml`): la ruta lee el cuerpo crudo y no lo
declara como parámetro, así que `publicar_esquemas` lo agrega al contrato.

Solo se aceptan los nombres del contrato (camelCase): un campo con otro nombre, de más o con otro
tipo es inválido, y nada de lo que trae el cuerpo se copia a un error ni a un log.
"""

import json
from collections.abc import Iterable
from datetime import UTC
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic.alias_generators import to_camel
from pydantic_core.core_schema import ValidationInfo

from adaptador_telemetria.domain.telemetria.errores import ContratoInvalido
from adaptador_telemetria.domain.telemetria.modelos import EventoCanonico

# Los mismos patrones que `EventoParametricoObservado`.
IDENTIFICADOR = r"^[A-Za-z0-9._:-]{1,64}$"
ZONA = r"^[A-Z0-9]{2,16}$"
VALOR_OBSERVADO = r"^[0-9]{1,9}(\.[0-9]{1,3})?$"
# Cada tipo de evento se mide en una sola unidad.
UNIDAD_POR_TIPO = {"LLUVIA_ACUMULADA": "mm", "RETRASO_VUELO": "min"}


class _Entrada(BaseModel):
    """Lo que envía la fuente: solo los nombres del contrato y sin campos de más."""

    model_config = ConfigDict(alias_generator=to_camel, extra="forbid")


class Ubicacion(_Entrada):
    """Coordenadas del punto de medición; opcional. La evaluación no la usa (minimización)."""

    lat: Annotated[float, Field(ge=-90, le=90, examples=[4.711])]
    lon: Annotated[float, Field(ge=-180, le=180, examples=[-74.0721])]


class EventoExterno(_Entrada):
    """Contrato v1 del evento externo. El cuerpo no admite campos de más (422 CONTRATO_INVALIDO).

    `LLUVIA_ACUMULADA` va en `mm` y `RETRASO_VUELO` en `min`; otra combinación es un contrato
    inválido. `zona` es un código de municipio DANE sintético o un código de vuelo.
    """

    version_contrato: Annotated[Literal["1"], Field(description="Única versión soportada")]
    id_externo: Annotated[
        str,
        Field(
            pattern=IDENTIFICADOR,
            description="Identificador del evento en la fuente; con la fuente fija el eventoId",
            examples=["IDEAM-2026-1110-0001"],
        ),
    ]
    evento_origen: Annotated[
        str,
        Field(
            pattern=IDENTIFICADOR,
            description="Fenómeno o incidente al que pertenece la medición",
            examples=["EVT-2026-0913"],
        ),
    ]
    tipo_evento: Literal["LLUVIA_ACUMULADA", "RETRASO_VUELO"]
    valor_observado: Annotated[
        str,
        Field(pattern=VALOR_OBSERVADO, description="Texto decimal", examples=["120", "135.5"]),
    ]
    unidad: Literal["mm", "min"]
    zona: Annotated[str, Field(pattern=ZONA, examples=["11001", "AV204"])]
    observado_en: Annotated[
        AwareDatetime, Field(description="Instante de la medición, UTC (ISO 8601 con Z)")
    ]
    ubicacion: Ubicacion | None = None

    @field_validator("unidad")
    @classmethod
    def _unidad_del_tipo(cls, unidad: str, info: ValidationInfo) -> str:
        tipo = info.data.get("tipo_evento")
        if tipo is not None and UNIDAD_POR_TIPO[tipo] != unidad:
            raise ValueError("unidad incoherente con el tipo de evento")
        return unidad


MAX_CAMPOS = 20
MAX_LARGO_CAMPO = 64


def _campos(error: ValidationError) -> tuple[str, ...]:
    """Las rutas de los campos inválidos, sin repetir y nunca con sus valores."""
    rutas = (
        ".".join(str(parte) for parte in detalle["loc"])
        for detalle in error.errors(include_input=False, include_url=False, include_context=False)
    )
    return _acotar(rutas)


def _acotar(rutas: Iterable[str]) -> tuple[str, ...]:
    """Sin repetidos ni vacíos, y acotadas: el nombre de un campo de más lo escribe quien envía."""
    vistas: list[str] = []
    for ruta in rutas:
        ruta = ruta[:MAX_LARGO_CAMPO]
        if ruta and ruta not in vistas:
            vistas.append(ruta)
    return tuple(vistas[:MAX_CAMPOS])


def _nombres_ajenos(cuerpo: bytes) -> tuple[str, ...]:
    """Los campos cuyo nombre no es el del contrato. Pydantic tolera un `id_externo` junto a un
    `idExterno` (un nombre de atributo ya "conocido" no cuenta como campo de más); el contrato solo
    admite los nombres camelCase y se comprueba aparte, sobre un cuerpo que ya validó."""
    datos = json.loads(cuerpo)
    ajenos = [nombre for nombre in datos if nombre not in _ALIAS[EventoExterno]]
    ubicacion = datos.get("ubicacion")
    if isinstance(ubicacion, dict):
        ajenos += [f"ubicacion.{n}" for n in ubicacion if n not in _ALIAS[Ubicacion]]
    return _acotar(ajenos)


_ALIAS = {
    modelo: frozenset(campo.alias or nombre for nombre, campo in modelo.model_fields.items())
    for modelo in (EventoExterno, Ubicacion)
}


def validar_contrato(cuerpo: bytes, fuente: str) -> EventoCanonico:
    """El evento canónico del cuerpo, o `ContratoInvalido` con los campos que fallan."""
    try:
        evento = EventoExterno.model_validate_json(cuerpo)
    except ValidationError as error:
        # `from None`: la cadena de excepciones traería los valores recibidos.
        raise ContratoInvalido(_campos(error)) from None
    if ajenos := _nombres_ajenos(cuerpo):
        raise ContratoInvalido(ajenos)
    return EventoCanonico(
        fuente=fuente,
        id_externo=evento.id_externo,
        evento_origen=evento.evento_origen,
        tipo_evento=evento.tipo_evento,
        valor_observado=evento.valor_observado,
        unidad=evento.unidad,
        zona=evento.zona,
        observado_en=evento.observado_en.astimezone(UTC),
    )
