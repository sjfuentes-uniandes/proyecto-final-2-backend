"""Cliente interno del servicio `siniestros` para el reporte móvil (HU-M01)."""

from collections.abc import Awaitable, Callable, Collection
from typing import Any
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from solventa_common.errors import ErrorNegocio
from solventa_common.resiliencia import (
    DependenciaNoDisponible,
    Proteccion,
    SinProteccion,
    TiempoAgotado,
)

from bff_movil.adapters.outbound.clientes_http import ROL_CLIENTE
from bff_movil.domain.polizas.modelos import Poliza
from bff_movil.domain.sesion.errores import ServicioNoDisponible


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class _ErrorDto(_Dto):
    codigo: str
    mensaje: str
    detalles: dict[str, Any]


class SiniestrosHttp:
    def __init__(
        self,
        cliente: httpx.AsyncClient,
        base_url: str,
        timeout_ms: int,
        proteccion: Proteccion | None = None,
    ) -> None:
        self._cliente = cliente
        self._base_url = base_url.rstrip("/")
        self._timeout_ms = timeout_ms
        self._proteccion = proteccion or SinProteccion()

    async def limites(self, actor_id: str) -> dict[str, Any]:
        return await self._ejecutar(
            "siniestros.limites",
            lambda timeout_s: self._get_limites(actor_id, timeout_s),
        )

    async def crear_borrador(
        self,
        actor_id: str,
        cliente_id: UUID,
        poliza: Poliza,
        descripcion: str,
        clave: UUID,
    ) -> dict[str, Any]:
        return await self._ejecutar(
            "siniestros.borrador.crear",
            lambda timeout_s: self._crear_borrador(
                actor_id, cliente_id, poliza, descripcion, clave, timeout_s
            ),
        )

    async def agregar_evidencia(
        self, actor_id: str, borrador_id: UUID, solicitud: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._ejecutar(
            "siniestros.borrador.evidencia",
            lambda timeout_s: self._agregar_evidencia(actor_id, borrador_id, solicitud, timeout_s),
        )

    async def _ejecutar[T](self, clave: str, llamada: Callable[[float], Awaitable[T]]) -> T:
        try:
            return await self._proteccion.ejecutar(
                clave, "siniestros", llamada, self._timeout_ms, None
            )
        except TiempoAgotado as error:
            raise ServicioNoDisponible("TIEMPO_AGOTADO") from error
        except DependenciaNoDisponible as error:
            raise ServicioNoDisponible("ERROR") from error

    async def _get_limites(self, actor_id: str, timeout_s: float) -> dict[str, Any]:
        respuesta = await self._cliente.get(
            f"{self._base_url}/v1/siniestros/limites",
            headers=self._cabeceras(actor_id),
            timeout=timeout_s,
        )
        return self._json_ok(respuesta)

    async def _crear_borrador(
        self,
        actor_id: str,
        cliente_id: UUID,
        poliza: Poliza,
        descripcion: str,
        clave: UUID,
        timeout_s: float,
    ) -> dict[str, Any]:
        respuesta = await self._cliente.post(
            f"{self._base_url}/v1/siniestros/borradores",
            headers={**self._cabeceras(actor_id), "Idempotency-Key": str(clave)},
            json={
                "clienteId": str(cliente_id),
                "descripcion": descripcion,
                "copiaPoliza": _copia_poliza(poliza),
            },
            timeout=timeout_s,
        )
        return self._json_ok(respuesta, estados_ok={200, 201})

    async def _agregar_evidencia(
        self, actor_id: str, borrador_id: UUID, solicitud: dict[str, Any], timeout_s: float
    ) -> dict[str, Any]:
        respuesta = await self._cliente.post(
            f"{self._base_url}/v1/siniestros/borradores/{borrador_id}/evidencias",
            headers=self._cabeceras(actor_id),
            json=solicitud,
            timeout=timeout_s,
        )
        return self._json_ok(respuesta, estados_ok={200, 201})

    @staticmethod
    def _cabeceras(actor_id: str) -> dict[str, str]:
        return {"X-Actor-Id": actor_id, "X-Actor-Rol": ROL_CLIENTE}

    @staticmethod
    def _json_ok(
        respuesta: httpx.Response, estados_ok: Collection[int] = frozenset({200})
    ) -> dict[str, Any]:
        if respuesta.status_code in estados_ok:
            try:
                cuerpo = respuesta.json()
            except ValueError as error:
                raise ServicioNoDisponible("ERROR") from error
            if not isinstance(cuerpo, dict):
                raise ServicioNoDisponible("ERROR")
            return cuerpo
        if respuesta.status_code < 500:
            try:
                error_dto = _ErrorDto.model_validate(respuesta.json())
            except ValueError as parse_error:
                raise ServicioNoDisponible("ERROR") from parse_error
            if respuesta.status_code not in {404, 409, 422}:
                raise ServicioNoDisponible("ERROR")
            raise ErrorNegocio(
                error_dto.codigo, error_dto.mensaje, respuesta.status_code, error_dto.detalles
            )
        raise ServicioNoDisponible("ERROR")


def _copia_poliza(poliza: Poliza) -> dict[str, Any]:
    """Proyección mínima de la póliza consultada, nunca aportada por el móvil."""
    return {
        "polizaId": str(poliza.poliza_id),
        "numero": poliza.numero,
        "estado": poliza.estado,
        "producto": poliza.producto_nombre,
        "ramo": poliza.ramo,
        "vigencia": {
            "desde": poliza.vigencia.desde.isoformat(),
            "hasta": poliza.vigencia.hasta.isoformat(),
        },
        "vigente": poliza.vigente,
        "prima": str(poliza.prima),
        "sumaAsegurada": str(poliza.suma_asegurada),
        "moneda": poliza.moneda,
        "periodicidad": poliza.periodicidad,
        "coberturas": [
            {
                "codigo": cobertura.codigo,
                "nombre": cobertura.nombre,
                "obligatoria": cobertura.obligatoria,
            }
            for cobertura in poliza.coberturas
        ],
    }
