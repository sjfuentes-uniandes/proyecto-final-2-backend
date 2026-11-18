"""Envio recuperable: preparar archivos sin conexion de BD y convertir en una transaccion."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from siniestros.application.siniestro.errores import (
    EvidenciaNoCargada,
    PolizaNoVigente,
    SolicitudInvalida,
)
from siniestros.application.siniestro.puertos import AlmacenEvidencias, Reloj
from siniestros.domain.siniestro.modelos import CopiaPoliza, TipoEvidencia

BOGOTA = timezone(timedelta(hours=-5))


def id_siniestro_movil(borrador_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"solventa/siniestro-movil/{borrador_id}")


def clave_permanente(borrador_id: UUID, evidencia_id: UUID) -> str:
    return f"siniestros/{id_siniestro_movil(borrador_id)}/{evidencia_id}"


@dataclass(frozen=True)
class EvidenciaEnvio:
    id: UUID
    nombre_archivo: str
    tipo: TipoEvidencia
    clave_objeto: str
    sha256: str
    capturada_en: datetime | None
    tamano_bytes: int
    tipo_contenido: str
    duracion_segundos: int | None
    estado_carga: str


@dataclass(frozen=True)
class BorradorEnvio:
    id: UUID
    actor_id: str
    cliente_id: UUID
    poliza_id: UUID
    descripcion: str
    causa: str | None
    fecha_evento: datetime | None
    version: int
    evidencias: tuple[EvidenciaEnvio, ...]
    resultado: dict[str, Any] | None


@dataclass(frozen=True)
class PolizaReporte:
    id: UUID
    estado: str
    vigente: bool
    copia: CopiaPoliza


class EnviosRepositorio(Protocol):
    async def preparar(self, actor_id: str, borrador_id: UUID, clave: UUID) -> BorradorEnvio: ...

    async def convertir(
        self, borrador: BorradorEnvio, clave: UUID, poliza: PolizaReporte | None, ahora: datetime
    ) -> dict[str, Any]: ...


def validar_reporte(borrador: BorradorEnvio, poliza: PolizaReporte | None, ahora: datetime) -> None:
    if poliza is None or poliza.id != borrador.poliza_id:
        raise SolicitudInvalida(["copiaPoliza"])
    vigencia = poliza.copia.vigencia
    hoy = ahora.astimezone(BOGOTA).date()
    if (
        poliza.estado != "ACTIVA"
        or not poliza.vigente
        or vigencia is None
        or not vigencia.desde <= hoy <= vigencia.hasta
    ):
        raise PolizaNoVigente
    campos = []
    if not borrador.descripcion.strip():
        campos.append("descripcion")
    if not borrador.causa or borrador.causa not in {a.codigo for a in poliza.copia.amparos}:
        campos.append("causa")
    fecha = borrador.fecha_evento
    if (
        fecha is None
        or fecha.utcoffset() is None
        or fecha > ahora
        or not vigencia.desde <= fecha.astimezone(BOGOTA).date() <= vigencia.hasta
    ):
        campos.append("fechaEvento")
    if not borrador.evidencias:
        campos.append("evidencias")
    if campos:
        raise SolicitudInvalida(campos)
    pendientes = [str(e.id) for e in borrador.evidencias if e.estado_carga != "CARGADA"]
    if pendientes:
        raise EvidenciaNoCargada(pendientes)


class EnviarBorrador:
    def __init__(
        self, repo: EnviosRepositorio, almacen: AlmacenEvidencias, reloj: Reloj, cifrado: str
    ) -> None:
        self._repo = repo
        self._almacen = almacen
        self._reloj = reloj
        self._cifrado = cifrado

    async def ejecutar(
        self, actor_id: str, borrador_id: UUID, clave: UUID, poliza: PolizaReporte | None
    ) -> dict[str, Any]:
        borrador = await self._repo.preparar(actor_id, borrador_id, clave)
        # La confirmacion es inmutable: un reintento no consulta S3 ni revalida la poliza.
        if borrador.resultado is None:
            validar_reporte(borrador, poliza, self._reloj.ahora())
            for evidencia in borrador.evidencias:
                await self._almacen.conservar(
                    evidencia.clave_objeto,
                    clave_permanente(borrador_id, evidencia.id),
                    evidencia.sha256,
                    evidencia.tamano_bytes,
                    evidencia.tipo_contenido,
                    self._cifrado,
                )
        return await self._repo.convertir(borrador, clave, poliza, self._reloj.ahora())
