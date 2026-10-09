"""Puertos del perfilamiento (02, CS-05).

Identidad, consentimiento y modelo pueden lanzar ClienteNoEncontrado, ModeloNoDisponible y
DependenciaNoDisponible. PuertoFuente.consultar no lanza por errores del proveedor: los
devuelve como estado. RepositorioPerfiles.guardar lanza ConflictoIdempotencia.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from cotizacion.domain.perfilamiento.modelo_riesgo import ModeloRiesgo
from cotizacion.domain.perfilamiento.modelos import (
    ContextoConsulta,
    EstadoIdentidad,
    Fuente,
    Perfil,
    ResultadoFuente,
    SolicitudFuente,
    VerificacionConsentimiento,
)


class PuertoIdentidad(Protocol):
    async def estado(self, cliente_id: UUID) -> EstadoIdentidad:
        """Estado de la verificación de identidad del cliente."""
        ...


class PuertoConsentimiento(Protocol):
    async def verificar(
        self, cliente_id: UUID, proposito: str, alcances: Sequence[Fuente]
    ) -> VerificacionConsentimiento:
        """Decisión del consentimiento vigente para el propósito y los alcances pedidos."""
        ...


class PuertoModeloRiesgo(Protocol):
    async def vigente(self, producto_id: str, en: datetime) -> ModeloRiesgo:
        """Modelo de riesgo vigente del producto en el instante dado."""
        ...

    async def por_version(self, version: str) -> ModeloRiesgo:
        """Modelo de riesgo con la versión exacta."""
        ...


class PuertoFuente(Protocol):
    async def consultar(
        self, solicitud: SolicitudFuente, contexto: ContextoConsulta
    ) -> ResultadoFuente:
        """Consulta una fuente; los errores del proveedor vuelven como estado."""
        ...


class EjecutorFuentes(Protocol):
    async def ejecutar(
        self, solicitudes: Sequence[SolicitudFuente], contexto: ContextoConsulta
    ) -> list[ResultadoFuente]:
        """Consulta las fuentes y devuelve los resultados en el orden de las solicitudes."""
        ...


class RepositorioPerfiles(Protocol):
    async def por_idempotencia(self, cliente_id: UUID, clave: UUID) -> Perfil | None:
        """Perfil creado antes con la misma clave de idempotencia."""
        ...

    async def guardar(self, perfil: Perfil) -> None:
        """Guarda el perfil inmutable y su linaje."""
        ...

    async def por_id(self, perfil_id: UUID) -> Perfil | None:
        """Perfil por identificador."""
        ...

    async def por_cliente(self, cliente_id: UUID, limite: int) -> list[Perfil]:
        """Últimos perfiles del cliente, del más reciente al más antiguo."""
        ...


class Reloj(Protocol):
    def ahora(self) -> datetime:
        """Instante actual en UTC."""
        ...
