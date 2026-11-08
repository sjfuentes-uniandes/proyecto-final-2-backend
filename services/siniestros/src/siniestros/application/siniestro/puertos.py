"""Puertos de la aplicación de siniestros (HU-W19). Los implementan los adaptadores de salida."""

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from siniestros.application.siniestro.vistas import (
    AccesoDenegado,
    DetalleLeido,
    FiltroBandeja,
    Indicadores,
    PaginaSiniestros,
)
from siniestros.domain.siniestro.modelos import (
    EntradaHistorial,
    Evidencia,
    IntegridadEvidencia,
    Siniestro,
    TipoSoporte,
)


class Reloj(Protocol):
    def ahora(self) -> datetime: ...


class SoportesRequeridos(Protocol):
    def para(self, amparo: str) -> tuple[TipoSoporte, ...]:
        """Los soportes que exige el amparo; vacío si no se conoce."""
        ...


class AlmacenEvidencias(Protocol):
    async def verificar(self, clave: str, sha256: str) -> IntegridadEvidencia:
        """Compara el checksum del objeto con `sha256` sin descargarlo. Nunca lanza: un objeto
        ausente es `NO_DISPONIBLE`; un fallo o un corte por tiempo, `NO_VERIFICABLE`."""
        ...

    async def firmar_lectura(self, clave: str, ttl_s: int, tipo_contenido: str) -> str:
        """URL prefirmada de lectura. Lanza `DependenciaNoDisponible` si no se puede firmar."""
        ...


@dataclass(frozen=True)
class RegistroIdempotencia:
    huella: str
    respuesta: dict[str, Any]


class TxSiniestros(Protocol):
    """Una transacción corta: ningún método espera a otro servicio ni a S3."""

    async def adquirir_idempotencia(self, clave: str) -> RegistroIdempotencia | None:
        """Serializa las solicitudes con la misma clave y devuelve lo que ya se guardó."""
        ...

    async def guardar_idempotencia(
        self, clave: str, huella: str, respuesta: dict[str, Any], creada_en: datetime
    ) -> None: ...

    async def siniestro(self, siniestro_id: UUID) -> Siniestro | None: ...

    async def reservar_siguiente(self) -> Siniestro | None:
        """El REPORTADO sin analista más antiguo, bloqueado (`FOR UPDATE SKIP LOCKED`)."""
        ...

    async def guardar_asignacion(self, siniestro: Siniestro, version_anterior: int) -> None: ...

    async def agregar_historial(self, entrada: EntradaHistorial) -> None: ...

    # Outbox: el evento se escribe en la misma transacción que el cambio o la consulta.
    async def evento_en_evaluacion(self, siniestro: Siniestro, ocurrido_en: datetime) -> None: ...

    async def evento_consultado(
        self, siniestro: Siniestro, actor_id: str, ocurrido_en: datetime
    ) -> None: ...

    async def evento_evidencia_consultada(
        self, evidencia: Evidencia, actor_id: str, expira_en: datetime, ocurrido_en: datetime
    ) -> None: ...

    async def evento_acceso_denegado(
        self, acceso: AccesoDenegado, ocurrido_en: datetime
    ) -> None: ...


class SiniestrosRepositorio(Protocol):
    def transaccion(self) -> AbstractAsyncContextManager[TxSiniestros]: ...

    async def bandeja(self, filtro: FiltroBandeja) -> PaginaSiniestros:
        """Lanza `SolicitudInvalida` si el cursor no es válido."""
        ...

    async def indicadores(
        self,
        actor_id: UUID,
        desde: datetime,
        hasta: datetime,
        corte: datetime,
        compromiso_horas: int,
    ) -> Indicadores: ...

    async def detalle(self, siniestro_id: UUID) -> DetalleLeido | None: ...

    async def evidencia(
        self, siniestro_id: UUID, evidencia_id: UUID
    ) -> tuple[bool, Evidencia | None]:
        """(¿existe el siniestro?, la evidencia si es de ese siniestro)."""
        ...
