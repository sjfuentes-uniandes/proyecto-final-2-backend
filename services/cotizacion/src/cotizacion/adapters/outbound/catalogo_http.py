"""PuertoModeloRiesgo sobre la API interna de catalogo (/v1/modelos-riesgo, 04 §B.4.1).

`DefinicionModeloCotizacion` es la copia del consumidor del esquema publicado en
contracts/modelos/modelo-riesgo.v1.schema.json (CT-W11-01 verifica que sean iguales). Una
respuesta fuera de ese esquema se trata como dependencia no disponible.

Caché: la versión vigente de cada producto vive `ttl_s` segundos (con 0 no se guarda); una
versión cerrada no cambia nunca y se guarda sin vencimiento. Un candado por clave evita que
varias solicitudes en frío pidan lo mismo a la vez.
"""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

from cotizacion.application.perfilamiento.puertos import Reloj
from cotizacion.domain.perfilamiento.errores import DependenciaNoDisponible, ModeloNoDisponible
from cotizacion.domain.perfilamiento.modelo_riesgo import (
    Condicion,
    DefAjuste,
    DefFactor,
    ModeloRiesgo,
    ReglaFactor,
)
from cotizacion.domain.perfilamiento.modelo_riesgo import Expresion as ExpresionDominio
from cotizacion.domain.perfilamiento.modelo_riesgo import UmbralNivel as UmbralDominio
from cotizacion.domain.perfilamiento.modelos import Direccion, Fuente, NivelRiesgo

DEPENDENCIA_MODELO = "catalogo.modelo"

_LITERAL = re.compile(r"-?\d+(\.\d+)?")

TextoDecimal = Annotated[str, Field(pattern=r"^-?\d+(\.\d+)?$")]
Operador = Literal["==", "!=", "<", "<=", ">", ">="]
CondicionDto = tuple[str, Operador, TextoDecimal]
DireccionDto = Literal["REDUCE_RIESGO", "NEUTRO", "AUMENTA_LEVEMENTE", "AUMENTA_RIESGO"]
Nivel = Literal["BAJO", "MEDIO", "ALTO"]


# Los nombres de las clases forman parte del esquema JSON ($defs): son los mismos de catalogo.
class _Estricto(BaseModel):
    model_config = ConfigDict(extra="forbid", alias_generator=to_camel)


class DefFuente(_Estricto):
    tipo: Literal["FINANZAS_ABIERTAS", "DATOS_ABIERTOS"]
    campos: list[str]


class Expresion(_Estricto):
    op: Literal["add", "sub", "mul", "div"]
    args: list["str | Expresion"]


class Regla(_Estricto):
    si: list[CondicionDto]
    contribucion: TextoDecimal
    direccion: DireccionDto


class Factor(_Estricto):
    codigo: str
    fuente: str
    peso: TextoDecimal
    reglas: list[Regla]


class Ajuste(_Estricto):
    codigo: str
    si: list[CondicionDto]
    efecto: Literal["FORZAR_NIVEL"]
    nivel: Nivel


class UmbralNivel(_Estricto):
    desde: TextoDecimal
    nivel: Nivel


class Confianza(_Estricto):
    penalizacion_no_autorizada: TextoDecimal
    penalizacion_no_obtenida: TextoDecimal


class DefinicionModeloCotizacion(_Estricto):
    model_config = ConfigDict(title="DefinicionModelo")
    producto_id: str
    proposito: str
    fuentes: dict[str, DefFuente]
    declarados: list[str]
    obligatorios: list[str]
    derivados: dict[str, Expresion]
    factores: list[Factor]
    ajustes: list[Ajuste]
    niveles: list[UmbralNivel]
    confianza: Confianza


class ModeloRiesgoDto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    version: str
    producto_id: str
    vigente_desde: datetime
    hash: str
    definicion: DefinicionModeloCotizacion


def _condiciones(condiciones: list[CondicionDto]) -> tuple[Condicion, ...]:
    return tuple(Condicion(v, o, Decimal(valor)) for v, o, valor in condiciones)


def _expresion(dto: Expresion) -> ExpresionDominio:
    args: list[str | Decimal | ExpresionDominio] = []
    for arg in dto.args:
        if isinstance(arg, Expresion):
            args.append(_expresion(arg))
        elif _LITERAL.fullmatch(arg):
            args.append(Decimal(arg))
        else:
            args.append(arg)
    return ExpresionDominio(op=dto.op, args=tuple(args))


def a_modelo_riesgo(dto: ModeloRiesgoDto) -> ModeloRiesgo:
    """Convierte la respuesta de catalogo a las dataclasses del dominio (CS-05)."""
    d = dto.definicion
    return ModeloRiesgo(
        version=dto.version,
        hash=dto.hash,
        producto_id=dto.producto_id,
        proposito=d.proposito,
        fuentes={Fuente(clave): tuple(f.campos) for clave, f in d.fuentes.items()},
        declarados=tuple(d.declarados),
        obligatorios=frozenset(d.obligatorios),
        derivados={nombre: _expresion(e) for nombre, e in d.derivados.items()},
        factores=tuple(
            DefFactor(
                codigo=f.codigo,
                fuente=f.fuente,
                peso=Decimal(f.peso),
                reglas=tuple(
                    ReglaFactor(
                        si=_condiciones(r.si),
                        contribucion=Decimal(r.contribucion),
                        direccion=Direccion(r.direccion),
                    )
                    for r in f.reglas
                ),
            )
            for f in d.factores
        ),
        ajustes=tuple(
            DefAjuste(a.codigo, _condiciones(a.si), a.efecto, NivelRiesgo(a.nivel))
            for a in d.ajustes
        ),
        niveles=tuple(UmbralDominio(Decimal(n.desde), NivelRiesgo(n.nivel)) for n in d.niveles),
        penalizacion_no_autorizada=Decimal(d.confianza.penalizacion_no_autorizada),
        penalizacion_no_obtenida=Decimal(d.confianza.penalizacion_no_obtenida),
    )


class CatalogoHttp:
    def __init__(
        self,
        cliente: httpx.AsyncClient,
        base_url: str,
        ttl_s: int,
        timeout_ms: int,
        reloj: Reloj,
    ) -> None:
        self._cliente = cliente
        self._base_url = base_url.rstrip("/")
        self._ttl = timedelta(seconds=ttl_s)
        self._timeout_s = timeout_ms / 1000
        self._reloj = reloj
        # clave → (modelo, vence en; None = no vence).
        self._cache: dict[str, tuple[ModeloRiesgo, datetime | None]] = {}
        self._candados: dict[str, asyncio.Lock] = {}

    async def vigente(self, producto_id: str, en: datetime) -> ModeloRiesgo:
        def pedir() -> Awaitable[ModeloRiesgo]:
            parametros = {"productoId": producto_id, "en": en.isoformat()}
            return self._pedir("/v1/modelos-riesgo/vigente", parametros)

        if self._ttl <= timedelta(0):
            return await pedir()
        return await self._con_cache(f"vigente:{producto_id}", self._ttl, pedir)

    async def por_version(self, version: str) -> ModeloRiesgo:
        return await self._con_cache(
            f"version:{version}", None, lambda: self._pedir(f"/v1/modelos-riesgo/{version}", None)
        )

    def _vigente_en_cache(self, clave: str) -> ModeloRiesgo | None:
        guardado = self._cache.get(clave)
        if guardado is None:
            return None
        modelo, vence = guardado
        return modelo if vence is None or self._reloj.ahora() < vence else None

    async def _con_cache(
        self,
        clave: str,
        ttl: timedelta | None,
        pedir: Callable[[], Awaitable[ModeloRiesgo]],
    ) -> ModeloRiesgo:
        modelo = self._vigente_en_cache(clave)
        if modelo is not None:
            return modelo
        async with self._candados.setdefault(clave, asyncio.Lock()):
            modelo = self._vigente_en_cache(clave)
            if modelo is None:
                modelo = await pedir()
                vence = None if ttl is None else self._reloj.ahora() + ttl
                self._cache[clave] = (modelo, vence)
        return modelo

    async def _pedir(self, ruta: str, parametros: dict[str, Any] | None) -> ModeloRiesgo:
        try:
            respuesta = await self._cliente.get(
                f"{self._base_url}{ruta}", params=parametros, timeout=self._timeout_s
            )
        except httpx.HTTPError as error:
            raise DependenciaNoDisponible(DEPENDENCIA_MODELO) from error
        if respuesta.status_code == 404:
            raise ModeloNoDisponible()
        if respuesta.status_code != 200:
            raise DependenciaNoDisponible(DEPENDENCIA_MODELO)
        try:
            return a_modelo_riesgo(ModeloRiesgoDto.model_validate_json(respuesta.content))
        except (ValidationError, ValueError) as error:
            raise DependenciaNoDisponible(DEPENDENCIA_MODELO) from error


if __name__ == "__main__":
    print(json.dumps(DefinicionModeloCotizacion.model_json_schema(), indent=2, sort_keys=True))
