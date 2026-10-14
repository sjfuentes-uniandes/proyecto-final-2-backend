"""Sobre y payloads v1 de los eventos del perfilamiento (01 §3.7, D1).

Ningún payload lleva valores financieros ni datos declarados: solo estados, códigos y nombres
de campos. Los esquemas publicados se regeneran con:
    uv run python -m cotizacion.adapters.outbound.eventos
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from cotizacion.domain.perfilamiento.modelos import (
    Direccion,
    EstadoFactor,
    EstadoFuente,
    EstadoPerfil,
    Fuente,
    MotivoDato,
    NivelRiesgo,
)

PERFIL_RIESGO_CALCULADO = "PerfilRiesgoCalculado"
DATOS_CONSENTIDOS_CONSULTADOS = "DatosConsentidosConsultados"


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)


class FactorEvento(_Dto):
    codigo: str
    direccion: Direccion | None
    estado: EstadoFactor


class DatoRequeridoEvento(_Dto):
    campo: str
    fuente: Fuente | None
    motivo: MotivoDato


class PerfilRiesgoCalculadoV1(_Dto):
    perfil_id: UUID
    cliente_id: UUID
    producto_id: str
    estado: EstadoPerfil
    nivel_riesgo: NivelRiesgo | None
    version_modelo: str
    hash_modelo: str
    factores: list[FactorEvento]
    datos_requeridos: list[DatoRequeridoEvento]
    perfil_anterior_id: UUID | None
    actor_id: str
    calculado_en: datetime


class FuenteConsultadaEvento(_Dto):
    fuente: Fuente
    estado: EstadoFuente
    campos_solicitados: list[str]
    campos_disponibles: list[str]
    version_fuente: str | None
    consultada_en: datetime | None


class DatosConsentidosConsultadosV1(_Dto):
    perfil_id: UUID
    cliente_id: UUID
    consentimiento_id: UUID | None
    proposito: str
    fuentes: list[FuenteConsultadaEvento]


class SobreEvento[P: (PerfilRiesgoCalculadoV1, DatosConsentidosConsultadosV1)](_Dto):
    """Sobre versionado: el consumidor elige el esquema del payload por `type` y `version`."""

    event_id: UUID
    aggregate_id: UUID
    type: str
    version: Literal[1]
    occurred_at: datetime
    correlation_id: str
    payload: P


ESQUEMAS: dict[str, type[BaseModel]] = {
    f"{PERFIL_RIESGO_CALCULADO}.v1.schema.json": SobreEvento[PerfilRiesgoCalculadoV1],
    f"{DATOS_CONSENTIDOS_CONSULTADOS}.v1.schema.json": SobreEvento[DatosConsentidosConsultadosV1],
}


if __name__ == "__main__":
    destino = Path("contracts/events")
    for nombre, modelo in ESQUEMAS.items():
        esquema = json.dumps(modelo.model_json_schema(by_alias=True), indent=2, sort_keys=True)
        (destino / nombre).write_text(esquema + "\n", encoding="utf-8")
        print(destino / nombre)
