"""ServicioRegistro sobre las rutas /v1/registros/{registroId} del servicio `clientes`."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, TypeVar
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from solventa_common.resiliencia import (
    DependenciaNoDisponible,
    Proteccion,
    SinProteccion,
    TiempoAgotado,
)

from bff_movil.adapters.outbound.clientes_http import ROL_CLIENTE
from bff_movil.domain.registro.modelos import (
    ClienteVinculado,
    RegistroConCorreoDistinto,
    RegistroVerificado,
    RegistroYaVinculado,
    TokenRegistroInvalido,
)
from bff_movil.domain.sesion.errores import ServicioNoDisponible
from bff_movil.domain.sesion.modelos import EstadoIdentidad
from bff_movil.domain.verificacion.modelos import ResultadoKyc

T = TypeVar("T")


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class _RegistroDto(_Dto):
    cliente_id: UUID
    estado: EstadoIdentidad
    resultado: ResultadoKyc
    motivo: str | None
    verificado_en: datetime | None
    token_registro: str | None
    expira_en: datetime | None


class _ClienteDto(_Dto):
    cliente_id: UUID
    estado: EstadoIdentidad
    verificado_en: datetime | None


def _error(respuesta: httpx.Response) -> tuple[str | None, str]:
    """Código estable y motivo del error de `clientes`; nunca el cuerpo completo."""
    try:
        cuerpo = respuesta.json()
        return cuerpo.get("codigo"), str(cuerpo.get("detalles", {}).get("motivo", "DESCONOCIDO"))
    except (ValueError, AttributeError):
        return None, "DESCONOCIDO"


class RegistroHttp:
    """Falla cerrado y nunca reintenta. No registra la selfie, el correo ni el token."""

    def __init__(
        self,
        cliente: httpx.AsyncClient,
        base_url: str,
        verificacion_timeout_ms: int,
        vinculacion_timeout_ms: int,
        proteccion_verificacion: Proteccion | None = None,
        proteccion_vinculacion: Proteccion | None = None,
    ) -> None:
        self._cliente = cliente
        self._base_url = base_url.rstrip("/")
        self._verificacion_timeout_ms = verificacion_timeout_ms
        self._vinculacion_timeout_ms = vinculacion_timeout_ms
        self._proteccion_verificacion = proteccion_verificacion or SinProteccion(
            corte_duro_ms=verificacion_timeout_ms
        )
        self._proteccion_vinculacion = proteccion_vinculacion or SinProteccion(
            corte_duro_ms=vinculacion_timeout_ms
        )

    async def verificar(
        self, registro_id: UUID, correo: str, selfie: str, prueba_de_vida: dict[str, Any]
    ) -> RegistroVerificado:
        async def llamada(timeout_s: float) -> RegistroVerificado:
            respuesta = await self._cliente.post(
                f"{self._base_url}/v1/registros/{registro_id}/verificaciones",
                json={"correo": correo, "selfie": selfie, "pruebaDeVida": prueba_de_vida},
                timeout=timeout_s,
            )
            return self._verificacion(respuesta)

        return await self._proteger(
            "clientes.registro.verificar",
            self._proteccion_verificacion,
            self._verificacion_timeout_ms,
            llamada,
        )

    async def vincular(self, registro_id: UUID, sub: str, token_registro: str) -> ClienteVinculado:
        async def llamada(timeout_s: float) -> ClienteVinculado:
            respuesta = await self._cliente.post(
                f"{self._base_url}/v1/registros/{registro_id}/vinculacion",
                json={"tokenRegistro": token_registro},
                headers={"X-Actor-Id": sub, "X-Actor-Rol": ROL_CLIENTE},
                timeout=timeout_s,
            )
            return self._vinculacion(respuesta)

        return await self._proteger(
            "clientes.registro.vincular",
            self._proteccion_vinculacion,
            self._vinculacion_timeout_ms,
            llamada,
        )

    async def _proteger(
        self,
        clave: str,
        proteccion: Proteccion,
        timeout_ms: int,
        llamada: Callable[[float], Awaitable[T]],
    ) -> T:
        try:
            return await proteccion.ejecutar(clave, "clientes", llamada, timeout_ms, None)
        except TiempoAgotado as error:
            raise ServicioNoDisponible("TIEMPO_AGOTADO") from error
        except DependenciaNoDisponible as error:
            raise ServicioNoDisponible("ERROR") from error

    @staticmethod
    def _verificacion(respuesta: httpx.Response) -> RegistroVerificado:
        if respuesta.status_code == 409:
            codigo, _ = _error(respuesta)
            if codigo == "REGISTRO_CORREO_DISTINTO":
                raise RegistroConCorreoDistinto()
            if codigo == "REGISTRO_YA_VINCULADO":
                raise RegistroYaVinculado()
        if respuesta.status_code != 200:
            raise ServicioNoDisponible("ERROR")
        try:
            dto = _RegistroDto.model_validate(respuesta.json())
        except ValueError as error:
            raise ServicioNoDisponible("ERROR") from error
        return RegistroVerificado(
            dto.cliente_id,
            dto.estado,
            dto.resultado,
            dto.motivo,
            dto.verificado_en,
            dto.token_registro,
            dto.expira_en,
        )

    @staticmethod
    def _vinculacion(respuesta: httpx.Response) -> ClienteVinculado:
        if respuesta.status_code in (403, 409):
            codigo, motivo = _error(respuesta)
            if respuesta.status_code == 403 and codigo == "TOKEN_REGISTRO_INVALIDO":
                raise TokenRegistroInvalido(motivo)
            if respuesta.status_code == 409 and codigo == "REGISTRO_YA_VINCULADO":
                raise RegistroYaVinculado()
        if respuesta.status_code != 200:
            raise ServicioNoDisponible("ERROR")
        try:
            dto = _ClienteDto.model_validate(respuesta.json())
        except ValueError as error:
            raise ServicioNoDisponible("ERROR") from error
        return ClienteVinculado(dto.cliente_id, dto.estado, dto.verificado_en)
