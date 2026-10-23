"""Insumos de la oferta para la suscripción automática de polizas (HU-W13, W13-B12).

Solo lectura: no cambia ninguna fila (las cotizaciones y los perfiles son inmutables). Reúne lo
mínimo que evalúan las reglas: la cotización (con la edad, nunca la fecha de nacimiento), su perfil
si lo usó (nivel, confianza, fuentes y coincidencias en listas restrictivas), el estado de identidad
del cliente, que se consulta a `clientes` (DS2-17), y el fin de la vigencia de la oferta (DS2-20):
desde HU-W04, el de la oferta guardada; sin ella (cotizaciones anteriores), el de HU-W13.
Una cotización vencida también se devuelve: la decide `polizas`.

Se mide con `operacion("cotizacion.insumos_suscripcion")`. La identidad y el perfil se piden en
paralelo; la identidad con el timeout de dependencia y falla cerrada (`clientes.identidad`). Las
conexiones de base se toman solo durante cada lectura.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from solventa_common.telemetry import RESULTADO_OK, RESULTADO_RECHAZADO, operacion

from cotizacion.application.cotizacion.puertos import RepositorioCotizaciones
from cotizacion.application.perfilamiento.puertos import PuertoIdentidad, RepositorioPerfiles
from cotizacion.domain.cotizacion.errores import CotizacionNoEncontrada, RolNoPermitido
from cotizacion.domain.cotizacion.modelos import Actor, Cotizacion, RolActor
from cotizacion.domain.cotizacion.oferta import vigente_hasta_de
from cotizacion.domain.perfilamiento.errores import ClienteNoEncontrado, DependenciaNoDisponible
from cotizacion.domain.perfilamiento.modelos import (
    EstadoCampo,
    EstadoFuente,
    EstadoIdentidad,
    Fuente,
    Perfil,
)

DEPENDENCIA_IDENTIDAD = "clientes.identidad"
CAMPO_LISTAS = "coincidencias_listas_restrictivas"


@dataclass(frozen=True, slots=True)
class ConfigInsumos:
    dependencia_timeout_ms: int = 150
    oferta_vigencia_horas: int = 96


@dataclass(frozen=True, slots=True)
class Insumos:
    cotizacion: Cotizacion
    estado_identidad: EstadoIdentidad
    vigente_hasta: datetime
    perfil: Perfil | None


def fuentes_consultadas(perfil: Perfil) -> int:
    return sum(1 for f in perfil.fuentes if f.estado is EstadoFuente.CONSULTADA)


def coincidencias_listas(perfil: Perfil | None) -> int | None:
    """Coincidencias en listas restrictivas de DA_REGISTROS_PUBLICOS; None si no se consultó o el
    dato no estuvo disponible."""
    if perfil is None:
        return None
    for fuente in perfil.fuentes:
        if (
            fuente.fuente is Fuente.DA_REGISTROS_PUBLICOS
            and fuente.estado is EstadoFuente.CONSULTADA
        ):
            campo = fuente.campos.get(CAMPO_LISTAS)
            if (
                campo is not None
                and campo.estado is EstadoCampo.DISPONIBLE
                and campo.valor is not None
            ):
                return int(campo.valor)
    return None


class ConsultarInsumos:
    def __init__(
        self,
        cotizaciones: RepositorioCotizaciones,
        perfiles: RepositorioPerfiles,
        identidad: PuertoIdentidad,
        config: ConfigInsumos,
    ) -> None:
        self._cotizaciones = cotizaciones
        self._perfiles = perfiles
        self._identidad = identidad
        self.config = config

    async def ejecutar(self, cotizacion_id: UUID, actor: Actor) -> Insumos:
        with operacion("cotizacion.insumos_suscripcion") as op:
            if actor.rol is not RolActor.ASESOR:
                op.marcar(RESULTADO_RECHAZADO)
                raise RolNoPermitido()
            cotizacion = await self._cotizaciones.por_id(cotizacion_id)
            if cotizacion is None:
                op.marcar(RESULTADO_RECHAZADO)
                raise CotizacionNoEncontrada()
            estado, perfil = await asyncio.gather(
                self._estado_identidad(cotizacion.cliente_id), self._perfil(cotizacion)
            )
        return Insumos(
            cotizacion=cotizacion,
            estado_identidad=estado,
            vigente_hasta=vigente_hasta_de(cotizacion, self.config.oferta_vigencia_horas),
            perfil=perfil,
        )

    async def _perfil(self, cotizacion: Cotizacion) -> Perfil | None:
        if cotizacion.perfil_id is None:
            return None
        return await self._perfiles.por_id(cotizacion.perfil_id)

    async def _estado_identidad(self, cliente_id: UUID) -> EstadoIdentidad:
        """Falla cerrada: un timeout, una falla técnica o un cliente que `clientes` no conoce (la
        cotización sí lo conoce) son DEPENDENCIA_NO_DISPONIBLE."""
        with operacion(f"cotizacion.dependencia.{DEPENDENCIA_IDENTIDAD}") as op:
            try:
                return await asyncio.wait_for(
                    self._identidad.estado(cliente_id), self.config.dependencia_timeout_ms / 1000
                )
            except (TimeoutError, DependenciaNoDisponible) as error:
                raise DependenciaNoDisponible(DEPENDENCIA_IDENTIDAD) from error
            except ClienteNoEncontrado as error:
                op.marcar(RESULTADO_OK)
                raise DependenciaNoDisponible(DEPENDENCIA_IDENTIDAD) from error
