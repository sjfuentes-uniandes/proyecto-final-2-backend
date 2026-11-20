"""Cliente interno del servicio `siniestros` para el reporte móvil (HU-M01)."""

from collections.abc import Awaitable, Callable, Collection
from typing import Any
from uuid import UUID

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
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


class _EnlaceDto(_Dto):
    url: str = Field(pattern=r"^https://")
    expira_en: AwareDatetime
    nombre_archivo: str = Field(min_length=1)
    tipo_contenido: str = Field(min_length=1)


class SiniestrosHttp:
    def __init__(
        self,
        cliente: httpx.AsyncClient,
        base_url: str,
        timeout_ms: int,
        proteccion: Proteccion | None = None,
        *,
        escritura_timeout_ms: int = 15000,
    ) -> None:
        self._cliente = cliente
        self._base_url = base_url.rstrip("/")
        self._timeout_ms = timeout_ms
        self._proteccion = proteccion or SinProteccion()
        self._escritura_timeout_ms = escritura_timeout_ms
        self._proteccion_escritura = proteccion or SinProteccion(corte_duro_ms=escritura_timeout_ms)

    async def _reporte(
        self,
        operacion: str,
        metodo: str,
        ruta: str,
        actor_id: str,
        cuerpo: dict[str, Any] | None = None,
        clave: UUID | None = None,
        params: dict[str, str] | None = None,
        *,
        escritura: bool = False,
    ) -> dict[str, Any]:
        async def llamar(timeout_s: float) -> dict[str, Any]:
            cabeceras = self._cabeceras(actor_id)
            if clave is not None:
                cabeceras["Idempotency-Key"] = str(clave)
            respuesta = await self._cliente.request(
                metodo,
                f"{self._base_url}{ruta}",
                headers=cabeceras,
                json=cuerpo,
                params=params,
                timeout=timeout_s,
            )
            if metodo == "DELETE" and respuesta.status_code == 204:
                return {}
            return self._json_ok(respuesta, estados_ok={200, 201})

        return await self._ejecutar(operacion, llamar, escritura=escritura)

    async def actualizar_borrador(
        self, actor_id: str, borrador_id: UUID, cambios: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._reporte(
            "siniestros.borrador.actualizar",
            "PATCH",
            f"/v1/siniestros/borradores/{borrador_id}",
            actor_id,
            cambios,
        )

    async def enviar_borrador(
        self, actor_id: str, borrador_id: UUID, clave: UUID, poliza: Poliza | None
    ) -> dict[str, Any]:
        return await self._reporte(
            "siniestros.borrador.enviar",
            "POST",
            f"/v1/siniestros/borradores/{borrador_id}/envio",
            actor_id,
            {"copiaPoliza": _copia_poliza(poliza) if poliza is not None else None},
            clave,
            escritura=True,
        )

    async def eliminar_evidencia(
        self, actor_id: str, borrador_id: UUID, evidencia_id: UUID
    ) -> None:
        await self._reporte(
            "siniestros.borrador.evidencia.eliminar",
            "DELETE",
            f"/v1/siniestros/borradores/{borrador_id}/evidencias/{evidencia_id}",
            actor_id,
        )

    async def listar_siniestros(
        self, actor_id: str, cliente_id: UUID, limite: int, cursor: str | None
    ) -> dict[str, Any]:
        params = {"limite": str(limite)}
        if cursor is not None:
            params["cursor"] = cursor
        return await self._reporte(
            "siniestros.propios.listar",
            "GET",
            f"/v1/clientes/{cliente_id}/siniestros",
            actor_id,
            params=params,
        )

    async def consultar_siniestro(
        self, actor_id: str, cliente_id: UUID, siniestro_id: UUID
    ) -> dict[str, Any]:
        return await self._reporte(
            "siniestros.propio.consultar",
            "GET",
            f"/v1/clientes/{cliente_id}/siniestros/{siniestro_id}",
            actor_id,
        )

    async def enlace_evidencia(
        self, actor_id: str, cliente_id: UUID, siniestro_id: UUID, evidencia_id: UUID
    ) -> dict[str, Any]:
        resultado = await self._reporte(
            "siniestros.evidencia.enlace",
            "POST",
            f"/v1/clientes/{cliente_id}/siniestros/{siniestro_id}/evidencias/{evidencia_id}/enlace",
            actor_id,
        )
        try:
            return _EnlaceDto.model_validate(resultado).model_dump(by_alias=True, mode="json")
        except ValueError as error:
            raise ServicioNoDisponible("ERROR") from error

    async def limites(self, actor_id: str) -> dict[str, Any]:
        return await self._ejecutar(
            "siniestros.limites",
            lambda timeout_s: self._get_limites(actor_id, timeout_s),
        )

    async def listar_borradores(self, actor_id: str) -> dict[str, Any]:
        return await self._ejecutar(
            "siniestros.borradores.listar",
            lambda timeout_s: self._get_borradores(actor_id, timeout_s),
        )

    async def consultar_borrador(self, actor_id: str, borrador_id: UUID) -> dict[str, Any]:
        return await self._ejecutar(
            "siniestros.borrador.consultar",
            lambda timeout_s: self._get_borrador(actor_id, borrador_id, timeout_s),
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

    async def confirmar_evidencia(
        self, actor_id: str, borrador_id: UUID, evidencia_id: UUID
    ) -> dict[str, Any]:
        return await self._ejecutar(
            "siniestros.borrador.evidencia.confirmar",
            lambda timeout_s: self._confirmar_evidencia(
                actor_id, borrador_id, evidencia_id, timeout_s
            ),
            escritura=True,
        )

    async def renovar_carga(
        self, actor_id: str, borrador_id: UUID, evidencia_id: UUID
    ) -> dict[str, Any]:
        return await self._ejecutar(
            "siniestros.borrador.evidencia.renovar",
            lambda timeout_s: self._renovar_carga(actor_id, borrador_id, evidencia_id, timeout_s),
        )

    async def _ejecutar[T](
        self, clave: str, llamada: Callable[[float], Awaitable[T]], *, escritura: bool = False
    ) -> T:
        proteccion = self._proteccion_escritura if escritura else self._proteccion
        timeout_ms = self._escritura_timeout_ms if escritura else self._timeout_ms
        try:
            return await proteccion.ejecutar(clave, "siniestros", llamada, timeout_ms, None)
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

    async def _get_borradores(self, actor_id: str, timeout_s: float) -> dict[str, Any]:
        respuesta = await self._cliente.get(
            f"{self._base_url}/v1/siniestros/borradores",
            headers=self._cabeceras(actor_id),
            timeout=timeout_s,
        )
        return self._json_ok(respuesta)

    async def _get_borrador(
        self, actor_id: str, borrador_id: UUID, timeout_s: float
    ) -> dict[str, Any]:
        respuesta = await self._cliente.get(
            f"{self._base_url}/v1/siniestros/borradores/{borrador_id}",
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

    async def _confirmar_evidencia(
        self, actor_id: str, borrador_id: UUID, evidencia_id: UUID, timeout_s: float
    ) -> dict[str, Any]:
        respuesta = await self._cliente.post(
            f"{self._base_url}/v1/siniestros/borradores/{borrador_id}/evidencias/"
            f"{evidencia_id}/confirmacion",
            headers=self._cabeceras(actor_id),
            timeout=timeout_s,
        )
        return self._json_ok(respuesta)

    async def _renovar_carga(
        self, actor_id: str, borrador_id: UUID, evidencia_id: UUID, timeout_s: float
    ) -> dict[str, Any]:
        respuesta = await self._cliente.post(
            f"{self._base_url}/v1/siniestros/borradores/{borrador_id}/evidencias/"
            f"{evidencia_id}/carga",
            headers=self._cabeceras(actor_id),
            timeout=timeout_s,
        )
        return self._json_ok(respuesta)

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
