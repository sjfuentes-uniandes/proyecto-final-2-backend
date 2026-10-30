"""ConsultaPolizas sobre GET /v1/clientes/{clienteId}/polizas del servicio `consulta` (HU-M10).

El clienteId lo resuelve el BFF desde el `sub`; el actor viaja en `X-Actor-Id` y
`X-Actor-Rol=CLIENTE`. Falla cerrado y nunca reintenta. No registra el cuerpo.
"""

from collections.abc import Awaitable, Callable
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, TypeAdapter
from pydantic.alias_generators import to_camel
from solventa_common.resiliencia import (
    DependenciaNoDisponible,
    Proteccion,
    SinProteccion,
    TiempoAgotado,
)

from bff_movil.adapters.outbound.clientes_http import ROL_CLIENTE
from bff_movil.domain.polizas.modelos import (
    Cobertura,
    Poliza,
    PolizaNoAutorizada,
    PolizaNoEncontrada,
    Vigencia,
)
from bff_movil.domain.sesion.errores import ServicioNoDisponible


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class _VigenciaDto(_Dto):
    desde: date
    hasta: date


class _CoberturaDto(_Dto):
    codigo: str
    nombre: str
    obligatoria: bool


class _PolizaDto(_Dto):
    poliza_id: UUID
    numero: str
    estado: str
    producto_nombre: str
    ramo: str
    vigencia: _VigenciaDto
    vigente: bool
    prima: Decimal
    suma_asegurada: Decimal
    moneda: str
    periodicidad: str
    coberturas: list[_CoberturaDto]
    documento_huella: str
    firma_id: str
    emitida_en: datetime

    def modelo(self) -> Poliza:
        return Poliza(
            poliza_id=self.poliza_id,
            numero=self.numero,
            estado=self.estado,
            producto_nombre=self.producto_nombre,
            ramo=self.ramo,
            vigencia=Vigencia(self.vigencia.desde, self.vigencia.hasta),
            vigente=self.vigente,
            prima=self.prima,
            suma_asegurada=self.suma_asegurada,
            moneda=self.moneda,
            periodicidad=self.periodicidad,
            coberturas=tuple(Cobertura(c.codigo, c.nombre, c.obligatoria) for c in self.coberturas),
            documento_huella=self.documento_huella,
            firma_id=self.firma_id,
            emitida_en=self.emitida_en,
        )


_LISTA = TypeAdapter(list[_PolizaDto])


def _codigo(respuesta: httpx.Response) -> str | None:
    try:
        codigo = respuesta.json().get("codigo")
    except (ValueError, AttributeError):
        return None
    return codigo if isinstance(codigo, str) else None


class PolizasHttp:
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

    async def listar(self, cliente_id: UUID, actor_id: str) -> list[Poliza]:
        return await self._ejecutar(
            "consulta.polizas",
            lambda timeout_s: self._listar(cliente_id, actor_id, timeout_s),
        )

    async def obtener(self, cliente_id: UUID, poliza_id: UUID, actor_id: str) -> Poliza:
        return await self._ejecutar(
            "consulta.poliza",
            lambda timeout_s: self._obtener(cliente_id, poliza_id, actor_id, timeout_s),
        )

    async def _ejecutar[T](self, clave: str, llamada: Callable[[float], Awaitable[T]]) -> T:
        try:
            return await self._proteccion.ejecutar(
                clave, "consulta", llamada, self._timeout_ms, None
            )
        except TiempoAgotado as error:
            raise ServicioNoDisponible("TIEMPO_AGOTADO") from error
        except DependenciaNoDisponible as error:
            raise ServicioNoDisponible("ERROR") from error

    def _cabeceras(self, actor_id: str) -> dict[str, str]:
        return {"X-Actor-Id": actor_id, "X-Actor-Rol": ROL_CLIENTE}

    async def _listar(self, cliente_id: UUID, actor_id: str, timeout_s: float) -> list[Poliza]:
        respuesta = await self._cliente.get(
            f"{self._base_url}/v1/clientes/{cliente_id}/polizas",
            headers=self._cabeceras(actor_id),
            timeout=timeout_s,
        )
        respuesta.raise_for_status()
        try:
            return [p.modelo() for p in _LISTA.validate_python(respuesta.json())]
        except ValueError as error:
            raise ServicioNoDisponible("ERROR") from error

    async def _obtener(
        self, cliente_id: UUID, poliza_id: UUID, actor_id: str, timeout_s: float
    ) -> Poliza:
        respuesta = await self._cliente.get(
            f"{self._base_url}/v1/clientes/{cliente_id}/polizas/{poliza_id}",
            headers=self._cabeceras(actor_id),
            timeout=timeout_s,
        )
        if respuesta.status_code == 404 and _codigo(respuesta) == "POLIZA_NO_ENCONTRADA":
            raise PolizaNoEncontrada()
        if respuesta.status_code == 403 and _codigo(respuesta) == "POLIZA_NO_AUTORIZADA":
            raise PolizaNoAutorizada()
        respuesta.raise_for_status()
        try:
            poliza = _PolizaDto.model_validate(respuesta.json())
        except ValueError as error:
            raise ServicioNoDisponible("ERROR") from error
        if poliza.poliza_id != poliza_id:
            raise ServicioNoDisponible("ERROR")
        return poliza.modelo()
