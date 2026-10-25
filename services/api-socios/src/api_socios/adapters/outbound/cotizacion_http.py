"""PuertoCotizacion sobre la API interna de `cotizacion` (/v1/cotizaciones, HU-W03 y HU-W04).

El socio es el actor: `X-Actor-Id` = su id (SOC-NNNNNN) y `X-Actor-Rol: SOCIO`; `cotizacion` solo
cotiza para él y solo le deja leer sus cotizaciones (la de otro socio responde 404, como si no
existiera). El correlationId y la traza se propagan con el cliente de `solventa_common`.

Lector tolerante: de la respuesta solo se valida lo que el socio recibe (`productoId` y `oferta`),
así un campo nuevo de `cotizacion` no rompe al canal. Los rechazos de negocio de `cotizacion` pasan
al socio con el mismo código y, de los detalles, solo `campos` (rutas, nunca valores). Cualquier
otra cosa (timeout, 5xx, un código que el socio no conoce o un cuerpo fuera del contrato) es
CotizacionNoDisponible: falla cerrada, sin exponer la topología interna.
"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel
from solventa_common import presupuesto
from solventa_common.correlation import cliente_async
from solventa_common.resiliencia import DependenciaNoDisponible, SinProteccion, TiempoAgotado

from api_socios.domain.ofertas import (
    CoberturaOferta,
    CotizacionNoDisponible,
    CotizacionRechazada,
    EstadoOferta,
    ExclusionResumida,
    Oferta,
    OfertaNoEncontrada,
    SolicitudOferta,
)

ROL_SOCIO = "SOCIO"
COTIZACION_NO_ENCONTRADA = "COTIZACION_NO_ENCONTRADA"
# Rechazos de POST /v1/cotizaciones que el socio puede provocar con su solicitud (codigos-error.md).
RECHAZOS_DEL_SOCIO = frozenset(
    {
        "SOLICITUD_INVALIDA",
        "IDEMPOTENCIA_CONFLICTO",
        "PRODUCTO_NO_ENCONTRADO",
        "PRODUCTO_INACTIVO",
        "PRODUCTO_FUERA_DE_VIGENCIA",
        "REGLAS_NO_VIGENTES",
        "DATOS_FUERA_DE_RANGO",
        "COBERTURA_NO_DISPONIBLE",
        "CLIENTE_NO_ENCONTRADO",
    }
)
TextoDecimal = Annotated[str, Field(pattern=r"^-?\d+(\.\d+)?$")]


class _Leido(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, extra="ignore")


class CoberturaLeida(_Leido):
    codigo: str
    nombre: str
    suma_asegurada: TextoDecimal


class ExclusionLeida(_Leido):
    codigo: str
    descripcion: str


class OfertaLeida(_Leido):
    id: UUID
    codigo: str
    cotizacion_id: UUID
    precio: TextoDecimal
    moneda: str
    periodicidad: str
    coberturas: list[CoberturaLeida]
    exclusiones_resumidas: list[ExclusionLeida]
    version: int
    version_reglas: str
    creada_en: datetime
    vigente_hasta: datetime
    estado: EstadoOferta


class CotizacionLeida(_Leido):
    producto_id: str
    oferta: OfertaLeida | None = None


def a_oferta(leida: CotizacionLeida) -> Oferta | None:
    o = leida.oferta
    if o is None:
        return None
    return Oferta(
        id=o.id,
        codigo=o.codigo,
        cotizacion_id=o.cotizacion_id,
        producto_id=leida.producto_id,
        precio=Decimal(o.precio),
        moneda=o.moneda,
        periodicidad=o.periodicidad,
        coberturas=tuple(
            CoberturaOferta(c.codigo, c.nombre, Decimal(c.suma_asegurada)) for c in o.coberturas
        ),
        exclusiones=tuple(
            ExclusionResumida(e.codigo, e.descripcion) for e in o.exclusiones_resumidas
        ),
        version=o.version,
        version_reglas=o.version_reglas,
        creada_en=o.creada_en,
        vigente_hasta=o.vigente_hasta,
        estado=o.estado,
    )


def cuerpo_solicitud(solicitud: SolicitudOferta) -> dict[str, Any]:
    """Sin socioId ni perfilId: el socio es el actor."""
    return {
        "clienteId": str(solicitud.cliente_id),
        "productoId": solicitud.producto_id,
        "datosAsegurado": {
            "fechaNacimiento": solicitud.fecha_nacimiento.isoformat(),
            "plazoCreditoMeses": solicitud.plazo_credito_meses,
            "sumaAsegurada": solicitud.suma_asegurada,
        },
        "coberturasOpcionales": list(solicitud.coberturas_opcionales),
    }


def _codigo_y_detalles(respuesta: httpx.Response) -> tuple[str | None, dict[str, Any]]:
    try:
        cuerpo = respuesta.json()
    except ValueError:
        return None, {}
    if not isinstance(cuerpo, dict):
        return None, {}
    codigo = cuerpo.get("codigo")
    detalles = cuerpo.get("detalles")
    campos = detalles.get("campos") if isinstance(detalles, dict) else None
    return (
        codigo if isinstance(codigo, str) else None,
        {"campos": campos} if isinstance(campos, list) else {},
    )


class CotizacionHttp:
    def __init__(
        self, url: str, timeout_ms: int, transporte: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._url = url.rstrip("/")
        self._timeout_ms = timeout_ms
        # HU-W05: el corte es el del socio (timeout_ms y su presupuesto), no el de 700 ms de
        # un aliado: cotizacion ya corta su propio recorrido.
        self._proteccion = SinProteccion(corte_duro_ms=timeout_ms)
        self._transporte = transporte

    async def cotizar(
        self, solicitud: SolicitudOferta, socio_id: str, clave: UUID
    ) -> tuple[Oferta, bool]:
        respuesta = await self._llamar(
            "POST",
            "/v1/cotizaciones",
            socio_id,
            json=cuerpo_solicitud(solicitud),
            headers={"Idempotency-Key": str(clave)},
        )
        if respuesta.status_code not in (200, 201):
            codigo, detalles = _codigo_y_detalles(respuesta)
            if respuesta.status_code in (404, 422) and codigo in RECHAZOS_DEL_SOCIO:
                raise CotizacionRechazada(codigo, respuesta.status_code, detalles)
            raise CotizacionNoDisponible(f"cotizacion respondió {respuesta.status_code}")
        oferta = self._oferta_de(respuesta)
        if oferta is None:
            # Una cotización recién calculada siempre trae su oferta (HU-W04).
            raise CotizacionNoDisponible("cotizacion respondió sin oferta")
        return oferta, respuesta.status_code == 201

    async def oferta(self, cotizacion_id: UUID, socio_id: str) -> Oferta:
        respuesta = await self._llamar("GET", f"/v1/cotizaciones/{cotizacion_id}", socio_id)
        if respuesta.status_code == 404:
            codigo, _ = _codigo_y_detalles(respuesta)
            if codigo == COTIZACION_NO_ENCONTRADA:
                raise OfertaNoEncontrada()
        if respuesta.status_code != 200:
            raise CotizacionNoDisponible(f"cotizacion respondió {respuesta.status_code}")
        oferta = self._oferta_de(respuesta)
        if oferta is None:
            # Cotización anterior a HU-W04: no tiene una oferta que el socio pueda presentar.
            raise OfertaNoEncontrada()
        return oferta

    async def _llamar(
        self, metodo: str, ruta: str, socio_id: str, **opciones: Any
    ) -> httpx.Response:
        encabezados = {
            "X-Actor-Id": socio_id,
            "X-Actor-Rol": ROL_SOCIO,
            **opciones.pop("headers", {}),
        }

        async def llamada(timeout_s: float) -> httpx.Response:
            # X-Deadline-Ms: lo que el socio seguirá esperando (HU-W05).
            encabezados.update(presupuesto.encabezado(timeout_s * 1000))
            async with cliente_async(
                base_url=self._url, timeout=timeout_s, transport=self._transporte
            ) as cliente:
                return await cliente.request(metodo, ruta, headers=encabezados, **opciones)

        try:
            return await self._proteccion.ejecutar(
                "cotizacion", "cotizacion", llamada, self._timeout_ms, presupuesto.restante_ms()
            )
        except (TiempoAgotado, DependenciaNoDisponible) as error:
            causa = error.__cause__ or error
            raise CotizacionNoDisponible(type(causa).__name__) from error

    @staticmethod
    def _oferta_de(respuesta: httpx.Response) -> Oferta | None:
        try:
            return a_oferta(CotizacionLeida.model_validate_json(respuesta.content))
        except ValidationError as error:
            raise CotizacionNoDisponible("respuesta fuera del contrato") from error
