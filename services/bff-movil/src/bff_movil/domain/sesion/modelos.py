"""Sesión del cliente: los servicios que puede usar según su estado de identidad."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID


class EstadoIdentidad(StrEnum):
    VERIFICADO = "VERIFICADO"
    PENDIENTE = "PENDIENTE"
    RECHAZADO = "RECHAZADO"


class ServicioAutorizado(StrEnum):
    POLIZAS = "POLIZAS"
    SINIESTROS = "SINIESTROS"
    PRIVACIDAD = "PRIVACIDAD"
    VERIFICACION_IDENTIDAD = "VERIFICACION_IDENTIDAD"


# TODO(HU-M07): catálogo provisional; reemplazarlo cuando se defina el de servicios del producto.
SERVICIOS_POR_ESTADO = MappingProxyType(
    {
        EstadoIdentidad.VERIFICADO: (
            ServicioAutorizado.POLIZAS,
            ServicioAutorizado.SINIESTROS,
            ServicioAutorizado.PRIVACIDAD,
        ),
        EstadoIdentidad.PENDIENTE: (ServicioAutorizado.VERIFICACION_IDENTIDAD,),
        EstadoIdentidad.RECHAZADO: (ServicioAutorizado.VERIFICACION_IDENTIDAD,),
    }
)


@dataclass(frozen=True, slots=True)
class Sesion:
    cliente_id: UUID
    servicios_autorizados: tuple[ServicioAutorizado, ...]
    expira_en: datetime


def servicios_autorizados(estado: EstadoIdentidad) -> tuple[ServicioAutorizado, ...]:
    """Servicios que el cliente puede usar con su estado de identidad."""
    return SERVICIOS_POR_ESTADO[estado]
