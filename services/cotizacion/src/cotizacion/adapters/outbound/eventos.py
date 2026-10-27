"""Sobre y payloads v1 de los eventos del perfilamiento (01 §3.7, D1) y de la decisión de
fallback de la cotización (HU-W06).

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

from cotizacion.domain.cotizacion.fallback import (
    AccionFallback,
    AccionSugerida,
    MensajeFallback,
    MotivoFalla,
    TipoDecision,
)
from cotizacion.domain.perfilamiento.modelos import (
    DecisionConsentimiento,
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
COTIZACION_DEGRADADA = "CotizacionDegradada"


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


class ComprobacionFinalEvento(_Dto):
    """Decisión de la comprobación final tras consultar las fuentes (HU-W31)."""

    decision: DecisionConsentimiento
    causa: str | None


class DatosConsentidosConsultadosV1(_Dto):
    perfil_id: UUID
    cliente_id: UUID
    consentimiento_id: UUID | None
    proposito: str
    fuentes: list[FuenteConsultadaEvento]
    # HU-W31 (opcionales, aditivos en v1): ausentes en los eventos anteriores a la comprobación.
    comprobacion_final: ComprobacionFinalEvento | None = None
    revocado_durante_perfilamiento: bool = False


class FaltanteEvento(_Dto):
    """Un dato que no se obtuvo: nombres y códigos, nunca un valor."""

    fuente: str
    dato: str
    motivo: MotivoFalla
    obligatoria: bool
    regla_id: str | None
    regla_version: str | None
    regla_accion: AccionFallback | None


class CotizacionDegradadaV1(_Dto):
    """Decisión de fallback del recorrido de cotización (HU-W06, AC3): DEGRADADA (oferta
    provisional, con su cotización) o PENDIENTE (sin oferta). Solo identificadores y motivos:
    sin cliente, montos ni datos declarados."""

    decision_id: UUID
    decision: TipoDecision
    cotizacion_id: UUID | None
    perfil_id: UUID
    producto_id: str
    faltantes: list[FaltanteEvento]
    mensaje_codigo: MensajeFallback
    accion_sugerida: AccionSugerida | None
    version_reglas_fallback: str | None
    hash_reglas_fallback: str | None
    actor_id: str
    decidido_en: datetime


class SobreEvento[
    P: (PerfilRiesgoCalculadoV1, DatosConsentidosConsultadosV1, CotizacionDegradadaV1)
](_Dto):
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
    f"{COTIZACION_DEGRADADA}.v1.schema.json": SobreEvento[CotizacionDegradadaV1],
}


if __name__ == "__main__":
    destino = Path("contracts/events")
    for nombre, modelo in ESQUEMAS.items():
        esquema = json.dumps(modelo.model_json_schema(by_alias=True), indent=2, sort_keys=True)
        (destino / nombre).write_text(esquema + "\n", encoding="utf-8")
        print(destino / nombre)
