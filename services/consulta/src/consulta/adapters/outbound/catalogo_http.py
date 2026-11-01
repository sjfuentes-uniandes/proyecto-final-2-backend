"""ConsultaCatalogo sobre `GET /v1/productos/{productoId}` de `catalogo` (HU-M10).

Lee la versión vigente del producto (el catálogo no selecciona por versión). Falla cerrado: un
timeout, un error, un 404 o una respuesta inválida son una dependencia no disponible, y el mensaje
se reintenta. Sin reintentos propios: el reintento es el de la cola.
"""

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic.alias_generators import to_camel
from solventa_common.resiliencia import (
    DependenciaNoDisponible as DependenciaCaida,
)
from solventa_common.resiliencia import (
    Proteccion,
    SinProteccion,
    TiempoAgotado,
)

from consulta.application.proyeccion.puertos import DependenciaNoDisponible, ProductoCatalogo
from consulta.domain.polizas.modelos import CoberturaProyectada

DEPENDENCIA = "catalogo"


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class _CoberturaDto(_Dto):
    codigo: str
    nombre: str
    obligatoria: bool


class _ProductoDto(_Dto):
    id: str
    version: str
    nombre: str
    ramo: str
    coberturas: list[_CoberturaDto]


class CatalogoHttp:
    """Implementa ConsultaCatalogo."""

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

    async def producto(self, producto_id: str) -> ProductoCatalogo:
        try:
            return await self._proteccion.ejecutar(
                "catalogo.producto",
                DEPENDENCIA,
                lambda timeout_s: self._consultar(producto_id, timeout_s),
                self._timeout_ms,
                None,
            )
        except (TiempoAgotado, DependenciaCaida, httpx.HTTPError, ValidationError) as error:
            raise DependenciaNoDisponible(DEPENDENCIA) from error

    async def _consultar(self, producto_id: str, timeout_s: float) -> ProductoCatalogo:
        respuesta = await self._cliente.get(
            f"{self._base_url}/v1/productos/{producto_id}", timeout=timeout_s
        )
        respuesta.raise_for_status()
        dto = _ProductoDto.model_validate_json(respuesta.content)
        return ProductoCatalogo(
            id=dto.id,
            version=dto.version,
            nombre=dto.nombre,
            ramo=dto.ramo,
            coberturas=tuple(
                CoberturaProyectada(c.codigo, c.nombre, c.obligatoria) for c in dto.coberturas
            ),
        )
