"""Consentimiento para el uso de datos (HU-M08) y su historial (HU-M09), tal como lo devuelve
`clientes`.

El BFF no decide nada del consentimiento: `clientes` valida, calcula la vigencia y deriva el
estado. Aquí solo viven la representación y los rechazos que la app traduce por `codigo`.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID


class EstadoConsentimiento(StrEnum):
    ACTIVO = "ACTIVO"
    REVOCADO = "REVOCADO"
    VENCIDO = "VENCIDO"


@dataclass(frozen=True, slots=True)
class Consentimiento:
    consentimiento_id: UUID
    estado: EstadoConsentimiento
    proposito: str
    alcances: tuple[str, ...]
    version_texto: str
    idioma: str
    otorgado_en: datetime
    vigente_hasta: datetime
    revocado_en: datetime | None


class TipoHito(StrEnum):
    OTORGADO = "OTORGADO"
    REVOCADO = "REVOCADO"
    VENCIDO = "VENCIDO"


@dataclass(frozen=True, slots=True)
class Hito:
    tipo: TipoHito
    ocurrido_en: datetime


@dataclass(frozen=True, slots=True)
class Historial:
    """Cuándo se otorgó y, si aplica, cuándo se revocó o venció (HU-M09 AC4). Lo deriva
    `clientes` de la fila inmutable; el BFF solo lo transporta."""

    consentimiento_id: UUID
    estado: EstadoConsentimiento
    hitos: tuple[Hito, ...]


@dataclass(frozen=True, slots=True)
class Otorgamiento:
    """Resultado de otorgar: `repeticion` si la misma clave ya había registrado el mismo cuerpo."""

    consentimiento: Consentimiento
    repeticion: bool


@dataclass(frozen=True, slots=True)
class SolicitudOtorgamiento:
    """Cuerpo tal como llega de la app: los faltantes los informa `clientes` (AC3)."""

    proposito: str | None
    alcances: tuple[str, ...] | None
    vigencia_meses: int | None
    version_texto: str | None
    idioma: str | None


@dataclass(frozen=True, slots=True)
class TextoVigente:
    proposito: str
    idioma: str
    version_texto: str
    titulo: str
    cuerpo: str
    aviso: str
    vigencias_meses: tuple[int, ...]
    vigencia_defecto_meses: int


class IdentidadNoVerificada(Exception):
    """Otorgar exige la identidad del titular `VERIFICADO`."""


class ConsentimientoNoEncontrado(Exception):
    """No existe o es de otro cliente: no se distingue."""


class ConsentimientoYaVigente(Exception):
    """Ya hay un consentimiento `ACTIVO` y vigente para el propósito."""

    def __init__(self, consentimiento_id: UUID) -> None:
        super().__init__(str(consentimiento_id))
        self.consentimiento_id = consentimiento_id


CodigoRechazo = Literal[
    "SOLICITUD_INVALIDA", "VIGENCIA_INVALIDA", "TEXTO_NO_VIGENTE", "IDEMPOTENCIA_CONFLICTO"
]
CODIGOS_RECHAZO: tuple[CodigoRechazo, ...] = (
    "SOLICITUD_INVALIDA",
    "VIGENCIA_INVALIDA",
    "TEXTO_NO_VIGENTE",
    "IDEMPOTENCIA_CONFLICTO",
)


class SolicitudRechazada(Exception):
    """`clientes` rechazó la solicitud (422). `campos` son rutas, nunca valores."""

    def __init__(self, codigo: CodigoRechazo, campos: tuple[str, ...] = ()) -> None:
        super().__init__(codigo)
        self.codigo: CodigoRechazo = codigo
        self.campos = campos
