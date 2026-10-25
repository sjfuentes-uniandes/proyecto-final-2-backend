"""PuertoProductos sobre catalogo (/v1/productos, HU-W03).

`DefinicionReglasPrimaCotizacion` es la copia del consumidor del esquema publicado en
contracts/modelos/reglas-prima.v1.schema.json (CT-W03-01 verifica que sean iguales). Una respuesta
fuera de ese esquema se trata como dependencia no disponible.

Caché (diagramas: catálogo actualiza / invalida, cotización lectura):
- Con `CACHE_URL`, se lee Valkey; las claves y el valor los escribe catalogo
  (`catalogo.adapters.outbound.productos_cache`) y son el JSON de su API. cotizacion nunca escribe:
  ante un fallo de caché pide a catalogo por HTTP, y es catalogo quien llena la caché.
- Sin `CACHE_URL`, una caché TTL en memoria por réplica, como CatalogoHttp (con 0 no se guarda).
Las reglas guardadas solo se usan si su vigenteDesde ≤ la fecha pedida.

HU-W05: cada GET a catalogo pasa por la `Proteccion` (por omisión SinProteccion; en la app,
ProteccionDependencia: reintento acotado ante conexión o 5xx, circuito por recurso y bulkhead
"catalogo") con el restante del presupuesto del recorrido, y envía ese timeout en X-Deadline-Ms.
Un corte (propio o el 504 de catalogo) es DependenciaAgotada.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel
from solventa_common import resiliencia
from solventa_common.cache import Cache

from cotizacion.adapters.outbound.http_protegido import llamar_protegido
from cotizacion.application.cotizacion.puertos import OrigenDato, Resuelto
from cotizacion.application.perfilamiento.puertos import Reloj
from cotizacion.domain.cotizacion import modelos as dominio
from cotizacion.domain.cotizacion.errores import ProductoNoEncontrado, ReglasNoVigentes
from cotizacion.domain.cotizacion.modelos import Cobertura, EstadoProducto, Producto, ReglasPrima
from cotizacion.domain.perfilamiento.errores import DependenciaNoDisponible

DEPENDENCIA = "catalogo.productos"
GRUPO = "catalogo"
CLAVE_PRODUCTO = "catalogo.producto"
CLAVE_REGLAS = "catalogo.reglas_prima"
PREFIJO = "catalogo:v1"
VARIABLES = frozenset({"edad", "plazo_credito_meses", "suma_asegurada"})

TextoDecimal = Annotated[str, Field(pattern=r"^-?\d+(\.\d+)?$")]
ModoRedondeo = Literal[
    "ROUND_HALF_EVEN", "ROUND_HALF_UP", "ROUND_HALF_DOWN", "ROUND_UP", "ROUND_DOWN"
]


# Los nombres de las clases forman parte del esquema JSON ($defs): son los mismos de catalogo.
class _Estricto(BaseModel):
    model_config = ConfigDict(extra="forbid", alias_generator=to_camel)


class FactoresNivel(BaseModel):
    """Factor por nivel de riesgo del perfil; SIN_PERFIL se aplica cuando no hay nivel."""

    # Sin alias: las claves son los niveles tal cual.
    model_config = ConfigDict(extra="forbid")
    BAJO: TextoDecimal
    MEDIO: TextoDecimal
    ALTO: TextoDecimal
    SIN_PERFIL: TextoDecimal


class Tramo(_Estricto):
    """Factor para los valores ≤ `hasta`; el último tramo es abierto (`hasta` nulo)."""

    hasta: TextoDecimal | None
    factor: TextoDecimal


class FactorVariable(_Estricto):
    codigo: str
    variable: str
    tramos: list[Tramo]


class Recargo(_Estricto):
    """Recargo de una cobertura opcional: `tasa` × prima ajustada."""

    cobertura: str
    tasa: TextoDecimal


class Redondeo(_Estricto):
    escala: Annotated[int, Field(ge=-6, le=6)]
    modo: ModoRedondeo


class Rango(_Estricto):
    minimo: TextoDecimal
    maximo: TextoDecimal


class Limites(_Estricto):
    suma_asegurada: Rango
    plazo_credito_meses: Rango
    edad: Rango


class SumaCobertura(_Estricto):
    """Parte de la suma asegurada que cubre una cobertura (0 < porcentaje ≤ 1); sin entrada, 1."""

    cobertura: str
    porcentaje: TextoDecimal


class Exclusion(_Estricto):
    """Exclusión resumida que el socio muestra al cliente; el texto legal está en el condicionado."""

    codigo: Annotated[str, Field(pattern=r"^[A-Z0-9_]{1,64}$")]
    descripcion: Annotated[str, Field(min_length=1, max_length=200)]


class CondicionesOferta(_Estricto):
    """Condiciones de la oferta que se crea con la prima (HU-W04).

    `vigenciaMinutos` cuenta desde el cálculo: vigente si ahora < calculadoEn + vigenciaMinutos.
    """

    vigencia_minutos: Annotated[int, Field(ge=1, le=525_600)]
    exclusiones: Annotated[list[Exclusion], Field(max_length=20)]
    sumas_coberturas: list[SumaCobertura]


class DefinicionReglasPrimaCotizacion(_Estricto):
    model_config = ConfigDict(title="DefinicionReglasPrima")
    producto_id: str
    moneda: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    periodicidad: Literal["MENSUAL", "ANUAL"]
    tasa_base: TextoDecimal
    factores_nivel: FactoresNivel
    factores_variables: list[FactorVariable]
    recargos: list[Recargo]
    prima_minima: TextoDecimal
    prima_maxima: TextoDecimal
    redondeo: Redondeo
    limites: Limites
    oferta: CondicionesOferta | None = Field(default=None, exclude_if=lambda v: v is None)


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class CoberturaDto(_Dto):
    codigo: str
    nombre: str
    obligatoria: bool


class ProductoDto(_Dto):
    id: str
    version: str
    nombre: str
    ramo: str
    pais: str
    moneda: str
    estado: EstadoProducto
    vigente_desde: datetime
    vigente_hasta: datetime | None
    coberturas: list[CoberturaDto]


class ReglasPrimaDto(_Dto):
    producto_id: str
    version: str
    vigente_desde: datetime
    hash: str
    definicion: DefinicionReglasPrimaCotizacion


def a_producto(dto: ProductoDto) -> Producto:
    return Producto(
        id=dto.id,
        version=dto.version,
        nombre=dto.nombre,
        ramo=dto.ramo,
        pais=dto.pais,
        moneda=dto.moneda,
        estado=dto.estado,
        vigente_desde=dto.vigente_desde,
        vigente_hasta=dto.vigente_hasta,
        coberturas=tuple(Cobertura(c.codigo, c.nombre, c.obligatoria) for c in dto.coberturas),
    )


def _rango(dto: Rango) -> dominio.Rango:
    return dominio.Rango(Decimal(dto.minimo), Decimal(dto.maximo))


def _condiciones(dto: CondicionesOferta | None) -> dominio.CondicionesOferta | None:
    if dto is None:
        return None
    return dominio.CondicionesOferta(
        vigencia_minutos=dto.vigencia_minutos,
        exclusiones=tuple(dominio.Exclusion(e.codigo, e.descripcion) for e in dto.exclusiones),
        porcentajes_suma={s.cobertura: Decimal(s.porcentaje) for s in dto.sumas_coberturas},
    )


def a_reglas(dto: ReglasPrimaDto) -> ReglasPrima:
    """Convierte la respuesta de catalogo al dominio. Una variable desconocida o un factor sin
    tramo abierto violan el contrato (catalogo los valida al publicar)."""
    d = dto.definicion
    for factor in d.factores_variables:
        if factor.variable not in VARIABLES or not factor.tramos or factor.tramos[-1].hasta:
            raise ValueError(f"factor fuera del contrato: {factor.codigo}")
    return ReglasPrima(
        producto_id=dto.producto_id,
        version=dto.version,
        hash=dto.hash,
        vigente_desde=dto.vigente_desde,
        moneda=d.moneda,
        periodicidad=d.periodicidad,
        tasa_base=Decimal(d.tasa_base),
        factores_nivel={k: Decimal(v) for k, v in d.factores_nivel.model_dump().items()},
        factores_variables=tuple(
            dominio.FactorVariable(
                codigo=f.codigo,
                variable=f.variable,
                tramos=tuple(
                    dominio.Tramo(
                        Decimal(t.hasta) if t.hasta is not None else None, Decimal(t.factor)
                    )
                    for t in f.tramos
                ),
            )
            for f in d.factores_variables
        ),
        recargos={r.cobertura: Decimal(r.tasa) for r in d.recargos},
        prima_minima=Decimal(d.prima_minima),
        prima_maxima=Decimal(d.prima_maxima),
        redondeo_escala=d.redondeo.escala,
        redondeo_modo=d.redondeo.modo,
        limites=dominio.Limites(
            suma_asegurada=_rango(d.limites.suma_asegurada),
            plazo_credito_meses=_rango(d.limites.plazo_credito_meses),
            edad=_rango(d.limites.edad),
        ),
        oferta=_condiciones(d.oferta),
    )


def _producto_de(contenido: bytes) -> Producto:
    return a_producto(ProductoDto.model_validate_json(contenido))


def _reglas_de(contenido: bytes) -> ReglasPrima:
    return a_reglas(ReglasPrimaDto.model_validate_json(contenido))


class ProductosCatalogo:
    def __init__(
        self,
        cliente: httpx.AsyncClient,
        base_url: str,
        timeout_ms: int,
        reloj: Reloj,
        cache: Cache | None = None,
        ttl_s: int = 60,
        proteccion: resiliencia.Proteccion | None = None,
    ) -> None:
        """Con `cache` (CACHE_URL) se lee Valkey; sin ella, la caché en memoria de `ttl_s`."""
        self._cliente = cliente
        self._base_url = base_url.rstrip("/")
        self._timeout_ms = timeout_ms
        self._proteccion = proteccion or resiliencia.SinProteccion()
        self._reloj = reloj
        self._cache = cache
        self._ttl = timedelta(seconds=ttl_s)
        # clave → (valor, vence en).
        self._memoria: dict[str, tuple[Producto | ReglasPrima, datetime]] = {}
        self._candados: dict[str, asyncio.Lock] = {}

    async def producto(self, producto_id: str) -> Resuelto[Producto]:
        resuelto = await self._resolver(
            f"{PREFIJO}:producto:{producto_id}",
            lambda: self._pedir(CLAVE_PRODUCTO, f"/v1/productos/{producto_id}", None, _producto_de),
            _producto_de,
        )
        if not isinstance(resuelto.valor, Producto):
            raise DependenciaNoDisponible(DEPENDENCIA)
        return Resuelto(resuelto.valor, resuelto.origen)

    async def reglas_vigentes(self, producto_id: str, en: datetime) -> Resuelto[ReglasPrima]:
        resuelto = await self._resolver(
            f"{PREFIJO}:reglas-prima:{producto_id}:vigente",
            lambda: self._pedir(
                CLAVE_REGLAS,
                f"/v1/productos/{producto_id}/reglas-prima/vigente",
                {"fecha": en.isoformat()},
                _reglas_de,
            ),
            _reglas_de,
            vigente_en=en,
        )
        if not isinstance(resuelto.valor, ReglasPrima):
            raise DependenciaNoDisponible(DEPENDENCIA)
        return Resuelto(resuelto.valor, resuelto.origen)

    async def _resolver(
        self,
        clave: str,
        pedir: Callable[[], Awaitable[Producto | ReglasPrima]],
        decodificar: Callable[[bytes], Producto | ReglasPrima],
        vigente_en: datetime | None = None,
    ) -> Resuelto[Producto | ReglasPrima]:
        def utilizable(valor: Producto | ReglasPrima) -> bool:
            return not (
                vigente_en is not None
                and isinstance(valor, ReglasPrima)
                and valor.vigente_desde > vigente_en
            )

        if self._cache is not None:
            guardado = await self._cache.leer(clave)
            if guardado is not None:
                try:
                    valor = decodificar(guardado)
                except (ValidationError, ValueError):
                    valor = None  # Ajeno al contrato: catalogo lo reemplaza al responder.
                if valor is not None and utilizable(valor):
                    return Resuelto(valor, OrigenDato.CACHE)
            return Resuelto(await pedir(), OrigenDato.CATALOGO)
        if self._ttl <= timedelta(0):
            return Resuelto(await pedir(), OrigenDato.CATALOGO)
        en_memoria = self._en_memoria(clave)
        if en_memoria is not None and utilizable(en_memoria):
            return Resuelto(en_memoria, OrigenDato.MEMORIA)
        async with self._candados.setdefault(clave, asyncio.Lock()):
            en_memoria = self._en_memoria(clave)
            if en_memoria is not None and utilizable(en_memoria):
                return Resuelto(en_memoria, OrigenDato.MEMORIA)
            valor = await pedir()
            self._memoria[clave] = (valor, self._reloj.ahora() + self._ttl)
        return Resuelto(valor, OrigenDato.CATALOGO)

    def _en_memoria(self, clave: str) -> Producto | ReglasPrima | None:
        guardado = self._memoria.get(clave)
        if guardado is None:
            return None
        valor, vence = guardado
        return valor if self._reloj.ahora() < vence else None

    async def _pedir(
        self,
        clave: str,
        ruta: str,
        parametros: dict[str, str] | None,
        decodificar: Callable[[bytes], Producto | ReglasPrima],
    ) -> Producto | ReglasPrima:
        respuesta = await llamar_protegido(
            self._cliente,
            self._proteccion,
            f"{self._base_url}{ruta}",
            parametros,
            clave=clave,
            grupo=GRUPO,
            timeout_ms=self._timeout_ms,
            dependencia=DEPENDENCIA,
        )
        if respuesta.status_code == 404:
            raise _no_encontrado(respuesta)
        if respuesta.status_code != 200:
            raise DependenciaNoDisponible(DEPENDENCIA)
        try:
            return decodificar(respuesta.content)
        except (ValidationError, ValueError) as error:
            raise DependenciaNoDisponible(DEPENDENCIA) from error


def _no_encontrado(respuesta: httpx.Response) -> Exception:
    """404 de catalogo: PRODUCTO_NO_ENCONTRADO o REGLAS_NO_VIGENTES; otro cuerpo viola el
    contrato."""
    try:
        codigo = respuesta.json().get("codigo")
    except (ValueError, AttributeError):
        codigo = None
    if codigo == "PRODUCTO_NO_ENCONTRADO":
        return ProductoNoEncontrado()
    if codigo == "REGLAS_NO_VIGENTES":
        return ReglasNoVigentes()
    return DependenciaNoDisponible(DEPENDENCIA)


if __name__ == "__main__":
    print(json.dumps(DefinicionReglasPrimaCotizacion.model_json_schema(), indent=2, sort_keys=True))
