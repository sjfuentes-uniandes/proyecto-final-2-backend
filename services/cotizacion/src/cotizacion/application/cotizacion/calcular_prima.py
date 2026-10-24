"""Caso de uso calcular_prima (HU-W03): calcula, persiste y devuelve una cotización inmutable.

Se mide con `operacion("cotizacion")` (meta p95 de `latency_targets` en `int`). Producto, reglas,
cliente y perfil se piden en paralelo; no se retiene una conexión de base mientras se espera a otros
servicios.

HU-W05: el recorrido abre un presupuesto (`presupuesto_recorrido_ms`, de LATENCY_BUDGET_MS, o el
X-Deadline-Ms recibido si es menor). Cada dependencia corta en min(`dependencia_timeout_ms`, corte
duro, restante − margen) y nunca pasa del restante; un corte es TIEMPO_AGOTADO y la
`PoliticaDependencias` decide qué hacer (hoy, falla cerrada: 503 DEPENDENCIA_NO_DISPONIBLE).

Orden de las causas (una sola, estable):
1. La tabla de decisión del producto (`elegibilidad`): inexistente, inactivo, fuera de vigencia.
2. Fallas técnicas (catalogo o clientes): DEPENDENCIA_NO_DISPONIBLE.
3. Reglas no vigentes; cliente inexistente; perfil inexistente o que no corresponde.
4. Coberturas y límites de las reglas; luego el cálculo.

HU-W04: con la prima se crea la oferta (precio, coberturas, exclusiones y vigencia de las reglas) y
se guarda con la cotización en una transacción.

El log de cierre lleva solo identificadores: ni montos, ni edad, ni fecha de nacimiento.
"""

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, NoReturn, Protocol, TypeVar
from uuid import UUID, uuid4

from solventa_common import presupuesto
from solventa_common.resiliencia import SinProteccion, TiempoAgotado, registrar_tiempo_agotado
from solventa_common.telemetry import (
    RESULTADO_ERROR,
    RESULTADO_OK,
    RESULTADO_RECHAZADO,
    RESULTADO_TIMEOUT,
    operacion,
)

from cotizacion.application.cotizacion.puertos import (
    OrigenDato,
    PuertoProductos,
    RepositorioCotizaciones,
    Resuelto,
)
from cotizacion.application.perfilamiento.puertos import (
    PuertoIdentidad,
    Reloj,
    RepositorioPerfiles,
)
from cotizacion.domain.cotizacion.elegibilidad import (
    coherentes,
    verificar_producto,
    verificar_reglas,
)
from cotizacion.domain.cotizacion.errores import (
    CoberturaNoDisponible,
    DatosFueraDeRango,
    PerfilNoCorresponde,
    ProductoFueraDeVigencia,
    ProductoInactivo,
    ProductoNoEncontrado,
    ReglasNoVigentes,
    RolNoPermitido,
)
from cotizacion.domain.cotizacion.modelos import (
    Actor,
    Cotizacion,
    EntradasPrima,
    EstadoCotizacion,
    Producto,
    ReglasPrima,
    RolActor,
)
from cotizacion.domain.cotizacion.oferta import crear_oferta
from cotizacion.domain.cotizacion.prima import (
    CAMPO_NACIMIENTO,
    CAMPO_PLAZO,
    CAMPO_SUMA,
    calcular_prima,
    edad_cumplida,
    huella_calculo,
    validar_entradas,
)
from cotizacion.domain.cotizacion.vigencia import VIGENCIA_HORAS_POR_OMISION
from cotizacion.domain.perfilamiento.errores import (
    ClienteNoEncontrado,
    ConflictoIdempotencia,
    DependenciaAgotada,
    DependenciaNoDisponible,
    PerfilNoEncontrado,
)
from cotizacion.domain.perfilamiento.modelos import Perfil

logger = logging.getLogger("cotizacion.cotizacion")

OPERACION = "cotizacion"
DEPENDENCIA_PRODUCTO = "catalogo.producto"
DEPENDENCIA_REGLAS = "catalogo.reglas_prima"
DEPENDENCIA_CLIENTE = "clientes.identidad"
DEPENDENCIA_PERFIL = "perfil"
CAMPO_PERFIL = "perfilId"
# Lo que el límite del caso de uso espera de más sobre el timeout de la dependencia.
HOLGURA_RED_MS = 10
# Proveedor de cada dependencia en el log del timeout (el grupo de su bulkhead).
PROVEEDORES = {
    DEPENDENCIA_PRODUCTO: "catalogo",
    DEPENDENCIA_REGLAS: "catalogo",
    DEPENDENCIA_CLIENTE: "clientes",
    DEPENDENCIA_PERFIL: "base-cotizacion",
}

# Errores de negocio (4xx): la operación queda "rechazado", no "error".
ERRORES_DE_NEGOCIO: tuple[type[Exception], ...] = (
    ProductoNoEncontrado,
    ProductoInactivo,
    ProductoFueraDeVigencia,
    ReglasNoVigentes,
    DatosFueraDeRango,
    CoberturaNoDisponible,
    PerfilNoCorresponde,
    PerfilNoEncontrado,
    ClienteNoEncontrado,
    ConflictoIdempotencia,
    RolNoPermitido,
)
# Respuestas funcionales de una dependencia que respondió bien.
_NO_EXISTE = (ProductoNoEncontrado, ReglasNoVigentes, ClienteNoEncontrado)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ComandoCotizacion:
    cliente_id: UUID
    producto_id: str
    fecha_nacimiento: date
    plazo_credito_meses: int
    suma_asegurada: Decimal
    coberturas_opcionales: tuple[str, ...]
    perfil_id: UUID | None
    socio_id: str | None
    idempotency_key: UUID
    actor: Actor
    correlation_id: str
    # X-Deadline-Ms recibido (api-socios, bff-web): acota el presupuesto propio.
    deadline_ms: int | None = None


@dataclass(frozen=True, slots=True)
class ConfigCotizacion:
    """`presupuesto_ms` es la meta p95 de la operación (contracts/operaciones.md) que se informa al
    portal; `presupuesto_recorrido_ms` es el corte del recorrido (HU-W05, LATENCY_BUDGET_MS)."""

    dependencia_timeout_ms: int = 150
    presupuesto_ms: int = 250
    presupuesto_recorrido_ms: int = 700
    # HU-W13 (DS2-20): horas de vigencia de la oferta desde el cálculo. Desde HU-W04 solo se usa
    # con reglas sin condiciones de oferta (las anteriores a 2026.10.1).
    oferta_vigencia_horas: int = VIGENCIA_HORAS_POR_OMISION


class ResultadoDependencia(StrEnum):
    """Cómo falló una dependencia del recorrido; lo recibe la PoliticaDependencias."""

    TIEMPO_AGOTADO = "TIEMPO_AGOTADO"
    NO_DISPONIBLE = "NO_DISPONIBLE"


class PoliticaDependencias(Protocol):
    """Punto de extensión de HU-W06 (proveedor degradado).

    Recibe cada falla técnica de una dependencia. Hoy toda política lanza; HU-W06 decidirá si
    alguna dependencia admite un valor sustituto (y cambiará el tipo de retorno).
    """

    def ante_falla(
        self, dependencia: str, resultado: ResultadoDependencia, error: BaseException
    ) -> NoReturn: ...


class FallaCerrada:
    """Política por omisión: cualquier falla técnica es 503 DEPENDENCIA_NO_DISPONIBLE."""

    def ante_falla(
        self, dependencia: str, resultado: ResultadoDependencia, error: BaseException
    ) -> NoReturn:
        if resultado is ResultadoDependencia.TIEMPO_AGOTADO:
            raise DependenciaAgotada(dependencia) from error
        raise DependenciaNoDisponible(dependencia) from error


@dataclass(frozen=True, slots=True)
class Paso:
    """Un paso del cálculo para el progreso del portal (mockup "Calculando tu oferta")."""

    codigo: str
    duracion_ms: int
    origen: OrigenDato | None = None


@dataclass(slots=True)
class _Dependencias:
    """Duración y resultado de cada dependencia, para el log de cierre (DT-11)."""

    registros: list[dict[str, Any]] = field(default_factory=list)
    duraciones: dict[str, float] = field(default_factory=dict)

    def agregar(self, nombre: str, ms: float, resultado: str) -> None:
        self.duraciones[nombre] = ms
        self.registros.append({"nombre": nombre, "ms": round(ms, 2), "resultado": resultado})


def huella_solicitud(cmd: ComandoCotizacion) -> str:
    """sha256 del JSON canónico de lo que identifica la solicitud (DT-26)."""
    contenido = {
        "clienteId": str(cmd.cliente_id),
        "productoId": cmd.producto_id,
        "perfilId": str(cmd.perfil_id) if cmd.perfil_id else None,
        "socioId": cmd.socio_id,
        "datosAsegurado": {
            "fechaNacimiento": cmd.fecha_nacimiento.isoformat(),
            "plazoCreditoMeses": cmd.plazo_credito_meses,
            "sumaAsegurada": str(cmd.suma_asegurada),
        },
        "coberturasOpcionales": sorted(set(cmd.coberturas_opcionales)),
    }
    canonico = json.dumps(contenido, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonico.encode("utf-8")).hexdigest()


def socio_de(cmd: ComandoCotizacion) -> str | None:
    """El socio de la cotización (0..1). Un socio solo cotiza para sí y sin perfiles: los perfiles
    son del back-office (/v1/perfiles exige ASESOR)."""
    if cmd.actor.rol is RolActor.SOCIO:
        if cmd.perfil_id is not None or cmd.socio_id not in (None, cmd.actor.id):
            raise RolNoPermitido()
        return cmd.actor.id
    return cmd.socio_id


def _verificar_perfil(cmd: ComandoCotizacion, perfil: Perfil) -> None:
    """El perfil sustenta la cotización: mismo cliente, mismo producto, mismos datos declarados."""
    declarados = perfil.datos_declarados
    campos = [
        campo
        for campo, coincide in (
            (CAMPO_PERFIL, perfil.cliente_id == cmd.cliente_id),
            (CAMPO_PERFIL, perfil.producto_id == cmd.producto_id),
            (CAMPO_NACIMIENTO, declarados.fecha_nacimiento == cmd.fecha_nacimiento),
            (CAMPO_PLAZO, declarados.plazo_credito_meses == cmd.plazo_credito_meses),
            (CAMPO_SUMA, declarados.suma_asegurada == cmd.suma_asegurada),
        )
        if not coincide
    ]
    if campos:
        raise PerfilNoCorresponde(tuple(dict.fromkeys(campos)))


class CalcularPrima:
    def __init__(
        self,
        productos: PuertoProductos,
        identidad: PuertoIdentidad,
        perfiles: RepositorioPerfiles,
        repo: RepositorioCotizaciones,
        reloj: Reloj,
        config: ConfigCotizacion,
        politica: PoliticaDependencias | None = None,
    ) -> None:
        self._productos = productos
        self._identidad = identidad
        self._perfiles = perfiles
        self._repo = repo
        self.reloj = reloj
        self.config = config
        self._politica = politica or FallaCerrada()
        # El perfil es una lectura de la base propia: solo timeout, sin reintento ni circuito.
        self._lectura_local = SinProteccion()

    async def ejecutar(self, cmd: ComandoCotizacion) -> tuple[Cotizacion, tuple[Paso, ...], bool]:
        """La cotización, sus pasos (vacíos si ya existía) y si se creó ahora."""
        with (
            operacion(OPERACION) as op,
            presupuesto.iniciar(self.config.presupuesto_recorrido_ms, cmd.deadline_ms),
        ):
            try:
                resultado = await self._ejecutar(cmd)
            except ERRORES_DE_NEGOCIO:
                op.marcar(RESULTADO_RECHAZADO)
                raise
            except DependenciaAgotada:
                # HU-W05: el recorrido falló porque una dependencia agotó su tiempo.
                op.marcar(RESULTADO_TIMEOUT)
                raise
        return resultado

    async def _ejecutar(self, cmd: ComandoCotizacion) -> tuple[Cotizacion, tuple[Paso, ...], bool]:
        socio = socio_de(cmd)
        huella = huella_solicitud(cmd)
        existente = await self._existente(cmd, huella)
        if existente is not None:
            return existente, (), False

        ahora = self.reloj.ahora()
        inicio = time.perf_counter()
        dependencias = _Dependencias()
        producto, reglas, perfil = await self._consultar(cmd, ahora, dependencias)
        inicio_calculo = time.perf_counter()
        entradas = EntradasPrima(
            suma_asegurada=cmd.suma_asegurada,
            plazo_credito_meses=cmd.plazo_credito_meses,
            edad=edad_cumplida(cmd.fecha_nacimiento, ahora.date()),
            nivel_riesgo=perfil.resultado.nivel if perfil else None,
            coberturas_opcionales=tuple(sorted(set(cmd.coberturas_opcionales))),
        )
        validar_entradas(producto.valor, reglas.valor, entradas)
        desglose = calcular_prima(reglas.valor, entradas)
        elegidas = set(entradas.coberturas_opcionales)
        cotizacion = Cotizacion(
            id=uuid4(),
            cliente_id=cmd.cliente_id,
            socio_id=socio,
            producto_id=producto.valor.id,
            version_producto=producto.valor.version,
            moneda=producto.valor.moneda,
            periodicidad=reglas.valor.periodicidad,
            version_reglas=reglas.valor.version,
            hash_reglas=reglas.valor.hash,
            perfil_id=cmd.perfil_id,
            entradas=entradas,
            coberturas=tuple(
                c for c in producto.valor.coberturas if c.obligatoria or c.codigo in elegidas
            ),
            desglose=desglose,
            huella_calculo=huella_calculo(reglas.valor, entradas, desglose),
            estado=EstadoCotizacion.CALCULADA,
            idempotency_key=cmd.idempotency_key,
            huella_solicitud=huella,
            calculado_en=ahora,
            duracion_ms=round((time.perf_counter() - inicio) * 1000),
            correlation_id=cmd.correlation_id,
            actor=cmd.actor,
        )
        cotizacion = replace(
            cotizacion,
            oferta=crear_oferta(
                cotizacion, reglas.valor, uuid4(), self.config.oferta_vigencia_horas
            ),
        )
        pasos = self._pasos(producto, reglas, perfil, dependencias, inicio_calculo)
        try:
            cotizacion = await self._repo.guardar(cotizacion)
        except ConflictoIdempotencia:
            # Carrera con otra solicitud de la misma clave: gana la que guardó primero.
            ganadora = await self._existente(cmd, huella)
            if ganadora is None:
                raise
            return ganadora, (), False
        logger.info(
            "cotizacion calculada",
            extra={
                "cotizacion_id": str(cotizacion.id),
                "oferta_id": str(cotizacion.oferta.id) if cotizacion.oferta else None,
                "producto_id": cotizacion.producto_id,
                "version_reglas": cotizacion.version_reglas,
                "perfil_id": str(cmd.perfil_id) if cmd.perfil_id else None,
                "dependencias": dependencias.registros,
            },
        )
        return cotizacion, pasos, True

    async def _existente(self, cmd: ComandoCotizacion, huella: str) -> Cotizacion | None:
        existente = await self._repo.por_idempotencia(cmd.actor.id, cmd.idempotency_key)
        if existente is not None and existente.huella_solicitud != huella:
            raise ConflictoIdempotencia()
        return existente

    async def _consultar(
        self, cmd: ComandoCotizacion, ahora: datetime, dependencias: _Dependencias
    ) -> tuple[Resuelto[Producto], Resuelto[ReglasPrima], Perfil | None]:
        """Producto, reglas, cliente y perfil en paralelo; decide en el orden del docstring."""
        producto_id = cmd.producto_id

        async def perfil() -> Perfil | None:
            perfil_id = cmd.perfil_id
            if perfil_id is None:
                return None
            try:
                return await self._lectura_local.ejecutar(
                    DEPENDENCIA_PERFIL,
                    PROVEEDORES[DEPENDENCIA_PERFIL],
                    lambda _timeout_s: self._perfiles.por_id(perfil_id),
                    self.config.dependencia_timeout_ms,
                    presupuesto.restante_ms(),
                )
            except TiempoAgotado as error:
                raise DependenciaAgotada(DEPENDENCIA_PERFIL) from error

        r_producto, r_reglas, r_cliente, r_perfil = await asyncio.gather(
            self._medir(
                DEPENDENCIA_PRODUCTO, lambda: self._productos.producto(producto_id), dependencias
            ),
            self._medir(
                DEPENDENCIA_REGLAS,
                lambda: self._productos.reglas_vigentes(producto_id, ahora),
                dependencias,
            ),
            self._medir(
                DEPENDENCIA_CLIENTE, lambda: self._identidad.estado(cmd.cliente_id), dependencias
            ),
            self._medir(DEPENDENCIA_PERFIL, perfil, dependencias),
            return_exceptions=True,
        )
        # 1. Tabla de decisión del producto.
        self._tecnica(DEPENDENCIA_PRODUCTO, r_producto)
        producto = _resuelto(r_producto, ProductoNoEncontrado)
        if producto is None:
            raise ProductoNoEncontrado()
        verificar_producto(producto.valor, ahora)
        # 2. Fallas técnicas.
        self._tecnica(DEPENDENCIA_REGLAS, r_reglas)
        self._tecnica(DEPENDENCIA_CLIENTE, r_cliente)
        self._tecnica(DEPENDENCIA_PERFIL, r_perfil)
        # 3. Reglas, cliente y perfil.
        reglas = _resuelto(r_reglas, (ReglasNoVigentes, ProductoNoEncontrado))
        if reglas is None:
            raise ReglasNoVigentes()
        verificar_reglas(reglas.valor, ahora)
        if not coherentes(producto.valor, reglas.valor):
            raise DependenciaNoDisponible(DEPENDENCIA_REGLAS)
        if isinstance(r_cliente, BaseException):
            raise r_cliente
        if isinstance(r_perfil, BaseException):
            raise r_perfil
        if cmd.perfil_id is not None:
            if r_perfil is None:
                raise PerfilNoEncontrado()
            _verificar_perfil(cmd, r_perfil)
        return producto, reglas, r_perfil

    def _tecnica(self, nombre: str, resultado: object) -> None:
        """Un timeout o una falla técnica de la dependencia: decide la política."""
        if isinstance(resultado, DependenciaAgotada | TimeoutError):
            self._politica.ante_falla(nombre, ResultadoDependencia.TIEMPO_AGOTADO, resultado)
        if isinstance(resultado, DependenciaNoDisponible):
            self._politica.ante_falla(nombre, ResultadoDependencia.NO_DISPONIBLE, resultado)

    async def _medir(
        self, nombre: str, llamada: Callable[[], Awaitable[T]], dependencias: _Dependencias
    ) -> T:
        """Mide la dependencia. El adaptador corta en su timeout efectivo, min(timeout, restante −
        margen); este límite es la red de seguridad (caché, candado, un puerto sin protección):
        min(timeout + holgura, restante), así el corte del adaptador llega primero."""
        inicio = time.perf_counter()
        resultado = RESULTADO_OK
        limite_ms = self.config.dependencia_timeout_ms + HOLGURA_RED_MS
        restante = presupuesto.restante_ms()
        if restante is not None:
            limite_ms = min(limite_ms, restante)
        try:
            with operacion(f"cotizacion.dependencia.{nombre}") as op:
                try:
                    async with asyncio.timeout(limite_ms / 1000):
                        return await llamada()
                except _NO_EXISTE:
                    # La dependencia respondió bien: el "no existe" es de negocio.
                    op.marcar(RESULTADO_OK)
                    raise
                except TimeoutError as error:
                    op.marcar(RESULTADO_TIMEOUT)
                    registrar_tiempo_agotado(PROVEEDORES.get(nombre, nombre), nombre, limite_ms, 0)
                    raise DependenciaAgotada(nombre) from error
                except DependenciaAgotada:
                    op.marcar(RESULTADO_TIMEOUT)
                    raise
        except DependenciaAgotada:
            resultado = RESULTADO_TIMEOUT
            raise
        except DependenciaNoDisponible:
            resultado = RESULTADO_ERROR
            raise
        finally:
            dependencias.agregar(nombre, (time.perf_counter() - inicio) * 1000, resultado)

    def _pasos(
        self,
        producto: Resuelto[Producto],
        reglas: Resuelto[ReglasPrima],
        perfil: Perfil | None,
        dependencias: _Dependencias,
        inicio_calculo: float,
    ) -> tuple[Paso, ...]:
        duracion = dependencias.duraciones
        catalogo_ms = max(duracion[DEPENDENCIA_PRODUCTO], duracion[DEPENDENCIA_REGLAS])
        origen = (
            OrigenDato.CATALOGO
            if OrigenDato.CATALOGO in (producto.origen, reglas.origen)
            else producto.origen
        )
        pasos = [
            Paso("CATALOGO_Y_REGLAS", round(catalogo_ms), origen),
            Paso("CLIENTE", round(duracion[DEPENDENCIA_CLIENTE])),
        ]
        if perfil is not None:
            pasos.append(Paso("PERFIL_RIESGO", round(duracion[DEPENDENCIA_PERFIL])))
        pasos.append(Paso("CALCULO_PRIMA", round((time.perf_counter() - inicio_calculo) * 1000)))
        return tuple(pasos)


def _resuelto[V](
    resultado: Resuelto[V] | BaseException, no_existe: type[Exception] | tuple[type[Exception], ...]
) -> Resuelto[V] | None:
    """El valor resuelto; None si la dependencia respondió "no existe"; cualquier otra excepción
    (un error de programación) se vuelve a lanzar."""
    if isinstance(resultado, no_existe):
        return None
    if isinstance(resultado, BaseException):
        raise resultado
    return resultado
