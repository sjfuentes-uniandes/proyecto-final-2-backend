"""ServicioConsentimientos sobre las rutas de consentimiento del servicio `clientes` (HU-M08).

`POST/GET /v1/clientes/actual/consentimientos`, `GET .../{consentimientoId}` y
`GET /v1/consentimientos/textos/vigente`. El titular viaja solo en `X-Actor-Id` (el `sub` del
token): el BFF nunca envía un `clienteId` ni acepta uno de la app.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, TypeVar, cast
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError
from pydantic.alias_generators import to_camel
from solventa_common.resiliencia import (
    DependenciaNoDisponible,
    Proteccion,
    SinProteccion,
    TiempoAgotado,
)

from bff_movil.adapters.outbound.clientes_http import ROL_CLIENTE
from bff_movil.domain.consentimientos.modelos import (
    CODIGOS_RECHAZO,
    CodigoRechazo,
    Consentimiento,
    ConsentimientoNoEncontrado,
    ConsentimientoYaVigente,
    EstadoConsentimiento,
    IdentidadNoVerificada,
    Otorgamiento,
    SolicitudOtorgamiento,
    SolicitudRechazada,
    TextoVigente,
)
from bff_movil.domain.sesion.errores import ClienteNoEncontrado, ServicioNoDisponible

T = TypeVar("T")

RUTA = "/v1/clientes/actual/consentimientos"
RUTA_TEXTO = "/v1/consentimientos/textos/vigente"
# Tope de rutas de campos que se reenvían a la app; nunca se reenvían valores.
MAX_CAMPOS = 32


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class _ConsentimientoDto(_Dto):
    consentimiento_id: UUID
    estado: EstadoConsentimiento
    proposito: str
    alcances: list[str]
    version_texto: str
    idioma: str
    otorgado_en: datetime
    vigente_hasta: datetime
    revocado_en: datetime | None

    def modelo(self) -> Consentimiento:
        return Consentimiento(
            self.consentimiento_id,
            self.estado,
            self.proposito,
            tuple(self.alcances),
            self.version_texto,
            self.idioma,
            self.otorgado_en,
            self.vigente_hasta,
            self.revocado_en,
        )


class _TextoVigenteDto(_Dto):
    proposito: str
    idioma: str
    version_texto: str
    titulo: str
    cuerpo: str
    aviso: str
    vigencias_meses: list[int]
    vigencia_defecto_meses: int


_LISTA = TypeAdapter(list[_ConsentimientoDto])


def _error(respuesta: httpx.Response) -> tuple[str | None, dict[str, Any]]:
    """Código estable y detalles del error de `clientes`; nunca el cuerpo completo."""
    try:
        cuerpo = respuesta.json()
        detalles = cuerpo.get("detalles")
        return cuerpo.get("codigo"), detalles if isinstance(detalles, dict) else {}
    except (ValueError, AttributeError):
        return None, {}


def _campos(detalles: dict[str, Any]) -> tuple[str, ...]:
    campos = detalles.get("campos")
    if not isinstance(campos, list):
        return ()
    return tuple(c for c in campos if isinstance(c, str))[:MAX_CAMPOS]


def _rechazo(respuesta: httpx.Response) -> SolicitudRechazada | None:
    """El 422 de `clientes` con un código conocido; cualquier otro 422 viola su contrato."""
    codigo, detalles = _error(respuesta)
    if codigo not in CODIGOS_RECHAZO:
        return None
    return SolicitudRechazada(cast(CodigoRechazo, codigo), _campos(detalles))


def _validar[R](modelo: Callable[[], R]) -> R:
    try:
        return modelo()
    except (ValueError, ValidationError) as error:
        raise ServicioNoDisponible("ERROR") from error


class ConsentimientosHttp:
    """Falla cerrado y nunca reintenta. No registra los cuerpos ni las cabeceras.

    Cada operación tiene su presupuesto: las lecturas usan el de `clientes`; otorgar es una
    escritura transaccional (dato, clave y outbox) y tolera un arranque en frío. Si se agota
    tras escribir, la app repite con la misma `Idempotency-Key` y recibe el mismo
    consentimiento.
    """

    def __init__(
        self,
        cliente: httpx.AsyncClient,
        base_url: str,
        consulta_timeout_ms: int,
        otorgamiento_timeout_ms: int,
        proteccion_consulta: Proteccion | None = None,
        proteccion_otorgamiento: Proteccion | None = None,
    ) -> None:
        self._cliente = cliente
        self._base_url = base_url.rstrip("/")
        self._consulta_timeout_ms = consulta_timeout_ms
        self._otorgamiento_timeout_ms = otorgamiento_timeout_ms
        self._proteccion_consulta = proteccion_consulta or SinProteccion()
        self._proteccion_otorgamiento = proteccion_otorgamiento or SinProteccion(
            corte_duro_ms=otorgamiento_timeout_ms
        )

    async def otorgar(
        self, sub: str, clave: UUID, solicitud: SolicitudOtorgamiento
    ) -> Otorgamiento:
        cuerpo = {
            "proposito": solicitud.proposito,
            "alcances": None if solicitud.alcances is None else list(solicitud.alcances),
            "vigenciaMeses": solicitud.vigencia_meses,
            "versionTexto": solicitud.version_texto,
            "idioma": solicitud.idioma,
        }

        async def llamada(timeout_s: float) -> Otorgamiento:
            respuesta = await self._cliente.post(
                f"{self._base_url}{RUTA}",
                json=cuerpo,
                headers={**self._actor(sub), "Idempotency-Key": str(clave)},
                timeout=timeout_s,
            )
            return self._otorgamiento(respuesta)

        return await self._proteger(
            "clientes.consentimientos.otorgar",
            self._proteccion_otorgamiento,
            self._otorgamiento_timeout_ms,
            llamada,
        )

    async def listar(self, sub: str) -> list[Consentimiento]:
        async def llamada(timeout_s: float) -> list[Consentimiento]:
            respuesta = await self._cliente.get(
                f"{self._base_url}{RUTA}", headers=self._actor(sub), timeout=timeout_s
            )
            if respuesta.status_code != 200:
                raise ServicioNoDisponible("ERROR")
            return [
                dto.modelo() for dto in _validar(lambda: _LISTA.validate_json(respuesta.content))
            ]

        return await self._proteger(
            "clientes.consentimientos.listar",
            self._proteccion_consulta,
            self._consulta_timeout_ms,
            llamada,
        )

    async def detalle(self, sub: str, consentimiento_id: UUID) -> Consentimiento:
        async def llamada(timeout_s: float) -> Consentimiento:
            respuesta = await self._cliente.get(
                f"{self._base_url}{RUTA}/{consentimiento_id}",
                headers=self._actor(sub),
                timeout=timeout_s,
            )
            if respuesta.status_code == 404 and _error(respuesta)[0] == (
                "CONSENTIMIENTO_NO_ENCONTRADO"
            ):
                raise ConsentimientoNoEncontrado()
            if respuesta.status_code != 200:
                raise ServicioNoDisponible("ERROR")
            consentimiento = self._consentimiento(respuesta)
            if consentimiento.consentimiento_id != consentimiento_id:
                raise ServicioNoDisponible("ERROR")
            return consentimiento

        return await self._proteger(
            "clientes.consentimientos.detalle",
            self._proteccion_consulta,
            self._consulta_timeout_ms,
            llamada,
        )

    async def texto_vigente(self, proposito: str | None, idioma: str | None) -> TextoVigente:
        parametros = {
            nombre: valor
            for nombre, valor in (("proposito", proposito), ("idioma", idioma))
            if valor is not None
        }

        async def llamada(timeout_s: float) -> TextoVigente:
            respuesta = await self._cliente.get(
                f"{self._base_url}{RUTA_TEXTO}", params=parametros, timeout=timeout_s
            )
            if respuesta.status_code == 422 and (rechazo := _rechazo(respuesta)):
                raise rechazo
            if respuesta.status_code != 200:
                raise ServicioNoDisponible("ERROR")
            dto = _validar(lambda: _TextoVigenteDto.model_validate_json(respuesta.content))
            return TextoVigente(
                dto.proposito,
                dto.idioma,
                dto.version_texto,
                dto.titulo,
                dto.cuerpo,
                dto.aviso,
                tuple(dto.vigencias_meses),
                dto.vigencia_defecto_meses,
            )

        return await self._proteger(
            "clientes.consentimientos.texto_vigente",
            self._proteccion_consulta,
            self._consulta_timeout_ms,
            llamada,
        )

    @staticmethod
    def _actor(sub: str) -> dict[str, str]:
        return {"X-Actor-Id": sub, "X-Actor-Rol": ROL_CLIENTE}

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
    def _consentimiento(respuesta: httpx.Response) -> Consentimiento:
        return _validar(lambda: _ConsentimientoDto.model_validate_json(respuesta.content)).modelo()

    @classmethod
    def _otorgamiento(cls, respuesta: httpx.Response) -> Otorgamiento:
        estado = respuesta.status_code
        if estado in (201, 200):
            # 200 solo es la repetición de la misma clave con el mismo cuerpo (AC4).
            return Otorgamiento(cls._consentimiento(respuesta), repeticion=estado == 200)
        codigo, detalles = _error(respuesta)
        if estado == 403 and codigo == "IDENTIDAD_NO_VERIFICADA":
            raise IdentidadNoVerificada()
        if estado == 404 and codigo == "CLIENTE_NO_ENCONTRADO":
            raise ClienteNoEncontrado()
        if estado == 409 and codigo == "CONSENTIMIENTO_YA_VIGENTE":
            existente = _validar(lambda: UUID(str(detalles.get("consentimientoId"))))
            raise ConsentimientoYaVigente(existente)
        if estado == 422 and (rechazo := _rechazo(respuesta)):
            raise rechazo
        # 401/403 de actor, 5xx (incluido DEPENDENCIA_NO_DISPONIBLE) o un código desconocido.
        raise ServicioNoDisponible("ERROR")
