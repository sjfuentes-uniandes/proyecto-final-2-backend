"""Casos de uso de la identidad del cliente."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from solventa_common import token_registro
from solventa_common.telemetry import (
    RESULTADO_ERROR,
    RESULTADO_RECHAZADO,
    RESULTADO_TIMEOUT,
    operacion,
)

from clientes.application.identidad.puertos import ConsultaVerificacion, RepositorioClientes
from clientes.domain.identidad.modelos import (
    Cliente,
    ClienteNoEncontrado,
    IdentidadNoDisponible,
    RegistroConCorreoDistinto,
    RegistroYaVinculado,
    ResultadoKyc,
    ResultadoVerificacion,
    TokenRegistroInvalido,
)

logger = logging.getLogger("clientes.identidad")


def _ahora() -> datetime:
    return datetime.now(UTC)


class VerificarIdentidad:
    def __init__(
        self,
        repositorio: RepositorioClientes,
        verificacion: ConsultaVerificacion,
        reloj: Callable[[], datetime] = _ahora,
    ) -> None:
        self._repositorio = repositorio
        self._verificacion = verificacion
        self._reloj = reloj

    async def ejecutar(
        self, sub: str, selfie: str, prueba_de_vida: dict[str, Any]
    ) -> ResultadoVerificacion:
        """Verifica la identidad del titular del `sub` y guarda el veredicto.

        El cliente se resuelve por `sub` y se crea una sola vez: repetir la verificación tras un
        error recuperable (AC3) reutiliza el mismo cliente. La selfie solo se reenvía al
        proveedor; nunca se guarda ni se registra.
        """
        cliente = await asyncio.to_thread(self._repositorio.asegurar, sub)
        return await self.verificar_cliente(cliente, selfie, prueba_de_vida)

    async def verificar_cliente(
        self, cliente: Cliente, selfie: str, prueba_de_vida: dict[str, Any]
    ) -> ResultadoVerificacion:
        """Consulta al proveedor y guarda el veredicto de un cliente ya resuelto."""
        with operacion("identidad.verificar") as op:
            if cliente.verificado:
                return ResultadoVerificacion(cliente, ResultadoKyc.APROBADO, None)
            try:
                veredicto = await self._verificacion.verificar(cliente.id, selfie, prueba_de_vida)
            except IdentidadNoDisponible as error:
                op.marcar(
                    RESULTADO_TIMEOUT if error.motivo == "TIEMPO_AGOTADO" else RESULTADO_ERROR
                )
                raise
            actualizado = cliente.con_resultado(veredicto.resultado, self._reloj())
            if actualizado != cliente:
                await asyncio.to_thread(self._repositorio.guardar, actualizado)
            if veredicto.resultado is not ResultadoKyc.APROBADO:
                op.marcar(RESULTADO_RECHAZADO)
            # Solo metadatos: nunca la selfie ni la prueba de vida.
            logger.info(
                "verificación procesada",
                extra={"cliente_id": str(cliente.id), "resultado": str(veredicto.resultado)},
            )
        return ResultadoVerificacion(actualizado, veredicto.resultado, veredicto.motivo)


@dataclass(frozen=True, slots=True)
class RegistroVerificado:
    verificacion: ResultadoVerificacion
    token: str | None
    expira_en: datetime | None


class VerificarRegistro:
    """Verifica la identidad antes de que exista la cuenta de Cognito (HU-M06)."""

    def __init__(
        self,
        repositorio: RepositorioClientes,
        verificador: VerificarIdentidad,
        secreto: str,
        reloj: Callable[[], datetime] = _ahora,
    ) -> None:
        self._repositorio = repositorio
        self._verificador = verificador
        self._secreto = secreto
        self._reloj = reloj

    async def ejecutar(
        self, registro_id: UUID, correo: str, selfie: str, prueba_de_vida: dict[str, Any]
    ) -> RegistroVerificado:
        """El cliente se resuelve por `registro_id` y se crea una sola vez (AC3).

        Solo una verificación exitosa devuelve el token que exige el pre sign-up de Cognito, y
        va ligado al correo: no sirve para crear la cuenta con otro.
        """
        correo = token_registro.normalizar_correo(correo)
        cliente = await asyncio.to_thread(self._repositorio.asegurar_registro, registro_id, correo)
        if cliente.correo != correo:
            raise RegistroConCorreoDistinto()
        if cliente.sub is not None:
            raise RegistroYaVinculado()
        resultado = await self._verificador.verificar_cliente(cliente, selfie, prueba_de_vida)
        if not resultado.cliente.verificado:
            return RegistroVerificado(resultado, None, None)
        token, expira_en = token_registro.emitir(
            self._secreto,
            str(registro_id),
            str(cliente.id),
            correo,
            ahora=int(self._reloj().timestamp()),
        )
        return RegistroVerificado(resultado, token, datetime.fromtimestamp(expira_en, UTC))


class VincularRegistro:
    """Asocia el `sub` de la cuenta recién creada con su cliente verificado (primer ingreso)."""

    def __init__(
        self,
        repositorio: RepositorioClientes,
        secreto: str,
        gracia_s: int,
        reloj: Callable[[], datetime] = _ahora,
    ) -> None:
        self._repositorio = repositorio
        self._secreto = secreto
        self._gracia_s = gracia_s
        self._reloj = reloj

    async def ejecutar(self, registro_id: UUID, sub: str, token: str) -> Cliente:
        with operacion("registro.vincular") as op:
            try:
                cliente = await self._vincular(registro_id, sub, token)
            except (TokenRegistroInvalido, RegistroYaVinculado):
                op.marcar(RESULTADO_RECHAZADO)
                raise
            logger.info("registro vinculado", extra={"cliente_id": str(cliente.id)})
        return cliente

    async def _vincular(self, registro_id: UUID, sub: str, token: str) -> Cliente:
        try:
            claims = token_registro.verificar(
                self._secreto,
                token,
                registro_id=str(registro_id),
                ahora=int(self._reloj().timestamp()),
                gracia_s=self._gracia_s,
            )
        except token_registro.TokenInvalido as error:
            raise TokenRegistroInvalido(error.motivo) from error
        cliente = await asyncio.to_thread(self._repositorio.por_registro, registro_id)
        if cliente is None or str(cliente.id) != claims.cliente_id:
            raise TokenRegistroInvalido("CLIENTE")
        if not cliente.verificado:
            raise TokenRegistroInvalido("NO_VERIFICADO")
        if cliente.sub == sub:
            return cliente
        if cliente.sub is not None:
            raise RegistroYaVinculado()
        return await asyncio.to_thread(self._repositorio.vincular, cliente.id, sub)


class ConsultarCliente:
    def __init__(self, repositorio: RepositorioClientes) -> None:
        self._repositorio = repositorio

    async def identidad(self, cliente_id: UUID) -> Cliente:
        cliente = await asyncio.to_thread(self._repositorio.obtener, cliente_id)
        if cliente is None:
            raise ClienteNoEncontrado()
        return cliente

    async def por_sub(self, sub: str) -> Cliente:
        cliente = await asyncio.to_thread(self._repositorio.por_sub, sub)
        if cliente is None:
            raise ClienteNoEncontrado()
        return cliente
