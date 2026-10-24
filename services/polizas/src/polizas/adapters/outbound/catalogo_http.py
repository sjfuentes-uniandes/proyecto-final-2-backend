"""ReglasSuscripcionPuerto sobre catalogo (`/v1/productos/{id}/reglas-suscripcion/...`, HU-W13).

Lector del contrato contracts/modelos/reglas-suscripcion.v1.schema.json: los conjuntos cerrados
(tipo, campo, operador, visibilidad, motivo y descriptor de las alternativas) se validan al leer;
una respuesta fuera de ellos es una dependencia no disponible (falla cerrada). Las alternativas
(HU-W14) son opcionales al leer: una definición sin ellas no tiene alternativas.

Caché TTL en memoria por réplica (DS2-18; polizas no tiene CACHE_URL): las reglas vigentes
`ttl_vigente_s` (60 s) y una versión cerrada `ttl_version_s` (24 h, no cambia nunca). Con 0 no se
guarda. Un candado por clave evita pedir lo mismo varias veces a la vez.
"""

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

from polizas.application.suscripcion.puertos import Reloj
from polizas.domain.suscripcion.errores import DependenciaNoDisponible, ReglasNoVigentes
from polizas.domain.suscripcion.modelos import (
    Alternativa,
    DescriptorAlternativa,
    Regla,
    ReglasSuscripcion,
    TipoRegla,
    Visibilidad,
)

DEPENDENCIA = "catalogo"

Campo = Literal[
    "edad",
    "suma_asegurada",
    "plazo_credito_meses",
    "nivel_riesgo",
    "confianza_perfil",
    "coincidencias_listas_restrictivas",
]
Operador = Literal[">=", "<=", "==", "!=", "entre", "en"]
Motivo = Literal[
    "EDAD_FUERA_DE_RANGO",
    "PLAZO_FUERA_DE_RANGO",
    "SUMA_REQUIERE_REVISION",
    "PERFIL_REQUIERE_REVISION",
    "NO_ELEGIBLE",
]
CodigoDescriptor = Literal["EDAD_MAXIMA", "SIN_LIMITE_EDAD"]
TextoDecimal = Annotated[str, Field(pattern=r"^-?\d+(\.\d+)?$")]


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ReglaDto(_Dto):
    codigo: str
    orden: int
    tipo: TipoRegla
    descripcion: str
    campo: Campo
    operador: Operador
    valor: str | Annotated[list[str], Field(min_length=1)]
    visibilidad: Visibilidad
    motivo_cliente: Motivo


class DescriptorAlternativaDto(_Dto):
    codigo: CodigoDescriptor
    valor: TextoDecimal | None


class AlternativaDto(_Dto):
    producto_id: str
    descriptor: DescriptorAlternativaDto


class DefinicionDto(_Dto):
    producto_id: str
    alternativas: list[AlternativaDto] = []
    reglas: Annotated[list[ReglaDto], Field(min_length=1)]


class ReglasSuscripcionDto(_Dto):
    producto_id: str
    version: str
    vigente_desde: datetime
    hash: str
    definicion: DefinicionDto


def a_reglas(contenido: bytes) -> ReglasSuscripcion:
    dto = ReglasSuscripcionDto.model_validate_json(contenido)
    return ReglasSuscripcion(
        producto_id=dto.producto_id,
        version=dto.version,
        hash=dto.hash,
        reglas=tuple(
            Regla(
                codigo=r.codigo,
                orden=r.orden,
                tipo=r.tipo,
                descripcion=r.descripcion,
                campo=r.campo,
                operador=r.operador,
                valor=r.valor if isinstance(r.valor, str) else tuple(r.valor),
                visibilidad=r.visibilidad,
                motivo_cliente=r.motivo_cliente,
            )
            for r in dto.definicion.reglas
        ),
        alternativas=tuple(
            Alternativa(
                a.producto_id, DescriptorAlternativa(a.descriptor.codigo, a.descriptor.valor)
            )
            for a in dto.definicion.alternativas
        ),
    )


class CatalogoHttp:
    """Implementa ReglasSuscripcionPuerto."""

    def __init__(
        self,
        cliente: httpx.AsyncClient,
        base_url: str,
        timeout_ms: int,
        reloj: Reloj,
        ttl_vigente_s: int = 60,
        ttl_version_s: int = 24 * 3600,
    ) -> None:
        self._cliente = cliente
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_ms / 1000
        self._reloj = reloj
        self._ttl_vigente = timedelta(seconds=ttl_vigente_s)
        self._ttl_version = timedelta(seconds=ttl_version_s)
        # clave → (reglas, vence en).
        self._memoria: dict[str, tuple[ReglasSuscripcion, datetime]] = {}
        self._candados: dict[str, asyncio.Lock] = {}

    async def vigentes(self, producto_id: str) -> ReglasSuscripcion:
        ruta = f"/v1/productos/{producto_id}/reglas-suscripcion/vigente"
        return await self._resolver(f"{producto_id}:vigente", ruta, self._ttl_vigente)

    async def por_version(self, producto_id: str, version: str) -> ReglasSuscripcion:
        ruta = f"/v1/productos/{producto_id}/reglas-suscripcion/{version}"
        return await self._resolver(f"{producto_id}:version:{version}", ruta, self._ttl_version)

    async def _resolver(self, clave: str, ruta: str, ttl: timedelta) -> ReglasSuscripcion:
        if ttl <= timedelta(0):
            return await self._pedir(ruta)
        if (guardadas := self._en_memoria(clave)) is not None:
            return guardadas
        async with self._candados.setdefault(clave, asyncio.Lock()):
            if (guardadas := self._en_memoria(clave)) is not None:
                return guardadas
            reglas = await self._pedir(ruta)
            self._memoria[clave] = (reglas, self._reloj.ahora() + ttl)
        return reglas

    def _en_memoria(self, clave: str) -> ReglasSuscripcion | None:
        guardado = self._memoria.get(clave)
        if guardado is None:
            return None
        reglas, vence = guardado
        return reglas if self._reloj.ahora() < vence else None

    async def _pedir(self, ruta: str) -> ReglasSuscripcion:
        return await _pedir(self._cliente, f"{self._base_url}{ruta}", self._timeout_s, a_reglas)


async def _pedir(
    cliente: httpx.AsyncClient,
    url: str,
    timeout_s: float,
    decodificar: Callable[[bytes], ReglasSuscripcion],
) -> ReglasSuscripcion:
    try:
        respuesta = await cliente.get(url, timeout=timeout_s)
    except httpx.HTTPError as error:
        raise DependenciaNoDisponible(DEPENDENCIA) from error
    if respuesta.status_code == 404 and _codigo(respuesta) == "REGLAS_NO_VIGENTES":
        raise ReglasNoVigentes()
    # Otro 404 (p. ej. PRODUCTO_NO_ENCONTRADO de un producto que cotizacion sí conoce) es una
    # incoherencia entre servicios: falla cerrada.
    if respuesta.status_code != 200:
        raise DependenciaNoDisponible(DEPENDENCIA)
    try:
        return decodificar(respuesta.content)
    except (ValidationError, ValueError) as error:
        raise DependenciaNoDisponible(DEPENDENCIA) from error


def _codigo(respuesta: httpx.Response) -> str | None:
    try:
        codigo = respuesta.json().get("codigo")
    except (ValueError, AttributeError):
        return None
    return codigo if isinstance(codigo, str) else None
