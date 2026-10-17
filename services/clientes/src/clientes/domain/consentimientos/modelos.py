"""Agregado Consentimiento (ADR 0009, 0010). Puro: sin E/S."""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class EstadoConsentimiento(StrEnum):
    ACTIVO = "ACTIVO"
    REVOCADO = "REVOCADO"
    VENCIDO = "VENCIDO"


PROPOSITO_PERFILAMIENTO = "PERFILAMIENTO_PRECIO"
PROPOSITOS = frozenset({PROPOSITO_PERFILAMIENTO})
# Solo Open Finance y Open Data (ADR 0001); coinciden con contracts/alcances-perfilamiento.json.
ALCANCES = frozenset(
    {
        "FA_PRODUCTOS_VIGENTES",
        "FA_HISTORIAL_PAGOS_12M",
        "FA_INGRESOS_AGREGADOS",
        "DA_REGISTROS_PUBLICOS",
    }
)
IDIOMAS = ("es-CO", "en-US")
# ADR 0005: el servidor define la lista; el cliente elige y la base calcula `vigenteHasta`.
VIGENCIAS_MESES = (6, 12)
VIGENCIA_DEFECTO_MESES = 12


def esta_vigente(estado: EstadoConsentimiento, vigente_hasta: datetime, ahora: datetime) -> bool:
    """Un consentimiento vigente está `ACTIVO` y `ahora` es anterior a `vigente_hasta`.

    `ahora` es el instante de la base de datos (ADR 0010); en el límite exacto ya no lo está.
    """
    return estado is EstadoConsentimiento.ACTIVO and ahora < vigente_hasta


@dataclass(frozen=True, slots=True)
class Consentimiento:
    """`estado` es el almacenado; `leido_en` es el instante de la base en que se leyó la fila.

    `VENCIDO` se deriva al leer y solo se materializa cuando un nuevo otorgamiento debe liberar
    el índice único (ADR 0009).
    """

    id: UUID
    cliente_id: UUID
    proposito: str
    alcances: tuple[str, ...]
    version_texto: str
    idioma: str
    estado: EstadoConsentimiento
    otorgado_en: datetime
    vigente_hasta: datetime
    revocado_en: datetime | None
    leido_en: datetime

    @property
    def vigente(self) -> bool:
        return esta_vigente(self.estado, self.vigente_hasta, self.leido_en)

    @property
    def estado_derivado(self) -> EstadoConsentimiento:
        if self.estado is EstadoConsentimiento.ACTIVO and not self.vigente:
            return EstadoConsentimiento.VENCIDO
        return self.estado


@dataclass(frozen=True, slots=True)
class SolicitudOtorgamiento:
    """Cuerpo de otorgar ya validado y normalizado."""

    proposito: str
    alcances: tuple[str, ...]
    vigencia_meses: int
    version_texto: str
    idioma: str

    @classmethod
    def desde(
        cls,
        *,
        proposito: str | None,
        alcances: Sequence[str] | None,
        vigencia_meses: int | None,
        version_texto: str | None,
        idioma: str | None,
    ) -> "SolicitudOtorgamiento":
        """Lanza SolicitudInvalida (AC3) con los campos en falta o inválidos y, si la solicitud
        está completa, VigenciaInvalida cuando la vigencia no es una de las permitidas."""
        campos: list[str] = []
        proposito_n = (proposito or "").strip().upper()
        if proposito_n not in PROPOSITOS:
            campos.append("proposito")
        alcances_n = tuple(sorted({a.strip().upper() for a in alcances or ()}))
        if not alcances_n or not set(alcances_n) <= ALCANCES:
            campos.append("alcances")
        if vigencia_meses is None:
            campos.append("vigenciaMeses")
        version_n = (version_texto or "").strip()
        if not version_n:
            campos.append("versionTexto")
        idioma_n = (idioma or "").strip()
        if idioma_n not in IDIOMAS:
            campos.append("idioma")
        if campos:
            raise SolicitudInvalida(campos)
        if vigencia_meses not in VIGENCIAS_MESES:
            raise VigenciaInvalida()
        assert vigencia_meses is not None
        return cls(proposito_n, alcances_n, vigencia_meses, version_n, idioma_n)

    def huella(self) -> str:
        """SHA-256 del JSON canónico del cuerpo normalizado (ADR 0009); los encabezados no entran."""
        canonico = json.dumps(
            {
                "proposito": self.proposito,
                "alcances": list(self.alcances),
                "vigenciaMeses": self.vigencia_meses,
                "versionTexto": self.version_texto,
                "idioma": self.idioma,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(canonico.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RegistroIdempotencia:
    huella: str
    consentimiento: Consentimiento


class SolicitudInvalida(Exception):
    """Faltan campos o son inválidos (`422 SOLICITUD_INVALIDA`, `detalles.campos`)."""

    def __init__(self, campos: Sequence[str]) -> None:
        super().__init__(", ".join(campos))
        self.campos = tuple(campos)


class VigenciaInvalida(Exception):
    """La vigencia no es 6 ni 12 meses (`422 VIGENCIA_INVALIDA`)."""


class TextoNoVigente(Exception):
    """La `versionTexto` no es la vigente (`422 TEXTO_NO_VIGENTE`)."""


class IdempotenciaConflicto(Exception):
    """La misma clave llegó con otro cuerpo (`422 IDEMPOTENCIA_CONFLICTO`)."""


class IdentidadNoVerificada(Exception):
    """Solo un titular con identidad `VERIFICADO` otorga (`403 IDENTIDAD_NO_VERIFICADA`)."""


class ConsentimientoYaVigente(Exception):
    """Ya hay un consentimiento `ACTIVO` y vigente para el propósito (`409`)."""

    def __init__(self, consentimiento_id: UUID) -> None:
        super().__init__(str(consentimiento_id))
        self.consentimiento_id = consentimiento_id


class ConsentimientoNoEncontrado(Exception):
    """No existe o es de otro cliente (`404 CONSENTIMIENTO_NO_ENCONTRADO`)."""


class ConsentimientoActivoExistente(Exception):
    """El índice único parcial rechazó un segundo `ACTIVO`: lo lanza el repositorio."""


class ClaveIdempotenciaEnUso(Exception):
    """Otra transacción registró antes la misma clave del cliente: lo lanza el repositorio."""
