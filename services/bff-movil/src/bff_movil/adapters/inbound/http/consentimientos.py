"""API del consentimiento para el uso de datos: otorgar, listar, detalle y texto vigente (HU-M08);
revocar e historial (HU-M09).

El titular es siempre el del token: ninguna ruta acepta un `clienteId` de la app. `clientes`
valida la solicitud y calcula la vigencia; el BFF solo traduce sus errores por `codigo`.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response
from pydantic import Field, StrictInt
from solventa_common.errors import ErrorNegocio

from bff_movil.adapters.inbound.http.auth import IdentidadAutenticada, autenticar
from bff_movil.adapters.inbound.http.codigos import (
    CLIENTE_NO_ENCONTRADO,
    CONSENTIMIENTO_NO_ENCONTRADO,
    CONSENTIMIENTO_YA_VIGENTE,
    IDENTIDAD_NO_VERIFICADA,
    SERVICIO_NO_DISPONIBLE,
)
from bff_movil.adapters.inbound.http.comun import CABECERAS, Dto, error
from bff_movil.application.consentimientos.casos import (
    ConsultarConsentimientos,
    ConsultarTextoVigente,
    OtorgarConsentimiento,
    RevocarConsentimiento,
)
from bff_movil.domain.consentimientos.modelos import (
    Consentimiento,
    ConsentimientoNoEncontrado,
    ConsentimientoYaVigente,
    EstadoConsentimiento,
    Historial,
    IdentidadNoVerificada,
    SolicitudOtorgamiento,
    SolicitudRechazada,
    TextoVigente,
    TipoHito,
)
from bff_movil.domain.sesion.errores import ClienteNoEncontrado, ServicioNoDisponible

router = APIRouter(prefix="/v1/consentimientos", tags=["consentimientos"])

IDEMPOTENT_REPLAYED = "Idempotent-Replayed"
_MENSAJES_RECHAZO = {
    "SOLICITUD_INVALIDA": "Solicitud inválida",
    "VIGENCIA_INVALIDA": "La vigencia no está permitida",
    "TEXTO_NO_VIGENTE": "El texto no es el vigente",
    "IDEMPOTENCIA_CONFLICTO": "La clave ya se usó con otro cuerpo",
}
_NO_AUTENTICADO = error(
    "NO_AUTENTICADO: falta el token, venció, tiene una firma inválida o no corresponde "
    "al titular autenticado. La app debe pedir autenticación completa"
)
_ROL = "ROL_NO_AUTORIZADO: el token no es del pool de clientes"


class SolicitudOtorgar(Dto):
    """Todos los campos son opcionales para el esquema: `clientes` informa en
    `detalles.campos` todos los que faltan o son inválidos, no solo el primero (AC3)."""

    proposito: Annotated[str | None, Field(max_length=64)] = None
    alcances: Annotated[list[Annotated[str, Field(max_length=64)]] | None, Field(max_length=16)] = (
        None
    )
    # Estricto: un booleano no es una vigencia (Pydantic lo convertiría en 0 o 1).
    vigencia_meses: StrictInt | None = None
    version_texto: Annotated[str | None, Field(max_length=64)] = None
    idioma: Annotated[str | None, Field(max_length=16)] = None

    def modelo(self) -> SolicitudOtorgamiento:
        return SolicitudOtorgamiento(
            proposito=self.proposito,
            alcances=None if self.alcances is None else tuple(self.alcances),
            vigencia_meses=self.vigencia_meses,
            version_texto=self.version_texto,
            idioma=self.idioma,
        )


class ConsentimientoDto(Dto):
    consentimiento_id: UUID
    estado: EstadoConsentimiento
    proposito: str
    alcances: list[str]
    version_texto: str
    idioma: str
    otorgado_en: datetime
    vigente_hasta: datetime
    revocado_en: datetime | None

    @classmethod
    def desde(cls, consentimiento: Consentimiento) -> "ConsentimientoDto":
        return cls(
            consentimiento_id=consentimiento.consentimiento_id,
            estado=consentimiento.estado,
            proposito=consentimiento.proposito,
            alcances=list(consentimiento.alcances),
            version_texto=consentimiento.version_texto,
            idioma=consentimiento.idioma,
            otorgado_en=consentimiento.otorgado_en,
            vigente_hasta=consentimiento.vigente_hasta,
            revocado_en=consentimiento.revocado_en,
        )


class HitoDto(Dto):
    tipo: TipoHito
    ocurrido_en: datetime


class HistorialDto(Dto):
    """Cuándo se otorgó y, si aplica, cuándo se revocó o venció (HU-M09 AC4)."""

    consentimiento_id: UUID
    estado: EstadoConsentimiento
    hitos: list[HitoDto]

    @classmethod
    def desde(cls, historial: Historial) -> "HistorialDto":
        return cls(
            consentimiento_id=historial.consentimiento_id,
            estado=historial.estado,
            hitos=[HitoDto(tipo=h.tipo, ocurrido_en=h.ocurrido_en) for h in historial.hitos],
        )


class TextoVigenteDto(Dto):
    proposito: str
    idioma: str
    version_texto: str
    titulo: str
    cuerpo: str
    aviso: str
    vigencias_meses: list[int]
    vigencia_defecto_meses: int

    @classmethod
    def desde(cls, texto: TextoVigente) -> "TextoVigenteDto":
        return cls(
            proposito=texto.proposito,
            idioma=texto.idioma,
            version_texto=texto.version_texto,
            titulo=texto.titulo,
            cuerpo=texto.cuerpo,
            aviso=texto.aviso,
            vigencias_meses=list(texto.vigencias_meses),
            vigencia_defecto_meses=texto.vigencia_defecto_meses,
        )


ConsentimientoId = Annotated[UUID, Path(alias="consentimientoId")]
Titular = Annotated[IdentidadAutenticada, Depends(autenticar)]


def _rechazo(falla: SolicitudRechazada) -> ErrorNegocio:
    detalles = {"campos": list(falla.campos)} if falla.codigo == "SOLICITUD_INVALIDA" else None
    return ErrorNegocio(falla.codigo, _MENSAJES_RECHAZO[falla.codigo], 422, detalles)


def _no_disponible() -> ErrorNegocio:
    return ErrorNegocio(SERVICIO_NO_DISPONIBLE, "Servicio no disponible", 503)


def _no_encontrado() -> ErrorNegocio:
    return ErrorNegocio(CONSENTIMIENTO_NO_ENCONTRADO, "El consentimiento no existe", 404)


_NO_ENCONTRADO = error(
    "CONSENTIMIENTO_NO_ENCONTRADO: no existe o es de otro cliente (no se distingue)"
)
_ID_INVALIDO = error("SOLICITUD_INVALIDA: el identificador o un encabezado no cumple su formato")


# Antes de /{consentimientoId}: si no, "texto-vigente" se leería como un identificador.
@router.get(
    "/texto-vigente",
    response_model=TextoVigenteDto,
    response_model_by_alias=True,
    summary="Texto de ejemplo vigente, su versión y las vigencias permitidas",
    responses={
        200: {"headers": CABECERAS},
        401: _NO_AUTENTICADO,
        403: error(_ROL),
        422: error("SOLICITUD_INVALIDA: proposito o idioma ausentes o no soportados"),
        503: error("SERVICIO_NO_DISPONIBLE: `clientes` no respondió"),
    },
)
async def texto_vigente(
    request: Request,
    _identidad: Titular,
    proposito: Annotated[str | None, Query(max_length=64)] = None,
    idioma: Annotated[str | None, Query(max_length=16)] = None,
) -> TextoVigenteDto:
    """Texto ilustrativo, sin validez legal (ADR 0005). No contiene datos del titular."""
    caso: ConsultarTextoVigente = request.app.state.consultar_texto_vigente
    try:
        return TextoVigenteDto.desde(await caso.ejecutar(proposito, idioma))
    except SolicitudRechazada as falla:
        raise _rechazo(falla) from falla
    except ServicioNoDisponible as falla:
        raise _no_disponible() from falla


@router.post(
    "",
    status_code=201,
    response_model=ConsentimientoDto,
    response_model_by_alias=True,
    summary="Otorga el consentimiento para consultar datos financieros",
    responses={
        201: {"headers": CABECERAS},
        200: {
            "model": ConsentimientoDto,
            "description": "Repetición: misma Idempotency-Key y mismo cuerpo. Devuelve la "
            "representación actual del consentimiento",
            "headers": {
                **CABECERAS,
                IDEMPOTENT_REPLAYED: {
                    "description": "Siempre `true`",
                    "schema": {"type": "string"},
                },
            },
        },
        400: error("ERROR_HTTP: el cuerpo no se puede leer como JSON"),
        401: _NO_AUTENTICADO,
        403: error(
            f"{_ROL}; o IDENTIDAD_NO_VERIFICADA: la identidad del titular no está VERIFICADA"
        ),
        404: error("CLIENTE_NO_ENCONTRADO: el titular aún no inició su verificación"),
        409: error(
            "CONSENTIMIENTO_YA_VIGENTE: ya hay uno ACTIVO para el propósito "
            "(detalles.consentimientoId)"
        ),
        422: error(
            "SOLICITUD_INVALIDA (detalles.campos), VIGENCIA_INVALIDA, TEXTO_NO_VIGENTE o "
            "IDEMPOTENCIA_CONFLICTO (misma Idempotency-Key con otro cuerpo)"
        ),
        503: error(
            "SERVICIO_NO_DISPONIBLE: no se pudo otorgar; la app informa y puede reintentar con "
            "la misma Idempotency-Key sin crear otro consentimiento"
        ),
    },
)
async def otorgar(
    solicitud: SolicitudOtorgar,
    request: Request,
    respuesta: Response,
    identidad: Titular,
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> ConsentimientoDto:
    """Crea el consentimiento `ACTIVO` del titular del token; `clientes` calcula `vigenteHasta`."""
    caso: OtorgarConsentimiento = request.app.state.otorgar_consentimiento
    try:
        otorgamiento = await caso.ejecutar(identidad.sub, idempotency_key, solicitud.modelo())
    except SolicitudRechazada as falla:
        raise _rechazo(falla) from falla
    except IdentidadNoVerificada as falla:
        raise ErrorNegocio(
            IDENTIDAD_NO_VERIFICADA, "La identidad no está verificada", 403
        ) from falla
    except ClienteNoEncontrado as falla:
        raise ErrorNegocio(CLIENTE_NO_ENCONTRADO, "El cliente no existe", 404) from falla
    except ConsentimientoYaVigente as falla:
        raise ErrorNegocio(
            CONSENTIMIENTO_YA_VIGENTE,
            "Ya hay un consentimiento vigente",
            409,
            {"consentimientoId": str(falla.consentimiento_id)},
        ) from falla
    except ServicioNoDisponible as falla:
        raise _no_disponible() from falla
    if otorgamiento.repeticion:
        respuesta.status_code = 200
        respuesta.headers[IDEMPOTENT_REPLAYED] = "true"
    return ConsentimientoDto.desde(otorgamiento.consentimiento)


@router.get(
    "",
    response_model=list[ConsentimientoDto],
    response_model_by_alias=True,
    summary="Consentimientos del titular, del más reciente al más antiguo",
    responses={
        200: {"headers": CABECERAS},
        401: _NO_AUTENTICADO,
        403: error(_ROL),
        422: error("SOLICITUD_INVALIDA: un encabezado no cumple su formato"),
        503: error("SERVICIO_NO_DISPONIBLE: `clientes` no respondió"),
    },
)
async def listar(request: Request, identidad: Titular) -> list[ConsentimientoDto]:
    """El estado es el derivado por `clientes` (`VENCIDO` si ya pasó `vigenteHasta`). Quien aún
    no tiene cliente recibe una lista vacía."""
    consultas: ConsultarConsentimientos = request.app.state.consultar_consentimientos
    try:
        return [ConsentimientoDto.desde(c) for c in await consultas.listar(identidad.sub)]
    except ServicioNoDisponible as falla:
        raise _no_disponible() from falla


@router.get(
    "/{consentimientoId}",
    response_model=ConsentimientoDto,
    response_model_by_alias=True,
    summary="Detalle y comprobante de un consentimiento del titular",
    responses={
        200: {"headers": CABECERAS},
        401: _NO_AUTENTICADO,
        403: error(_ROL),
        404: error(
            "CONSENTIMIENTO_NO_ENCONTRADO: no existe o es de otro cliente (no se distingue)"
        ),
        422: error("SOLICITUD_INVALIDA: el identificador o un encabezado no cumple su formato"),
        503: error("SERVICIO_NO_DISPONIBLE: `clientes` no respondió"),
    },
)
async def detalle(
    consentimiento_id: ConsentimientoId, request: Request, identidad: Titular
) -> ConsentimientoDto:
    consultas: ConsultarConsentimientos = request.app.state.consultar_consentimientos
    try:
        return ConsentimientoDto.desde(await consultas.detalle(identidad.sub, consentimiento_id))
    except ConsentimientoNoEncontrado as falla:
        raise ErrorNegocio(
            CONSENTIMIENTO_NO_ENCONTRADO, "El consentimiento no existe", 404
        ) from falla
    except ServicioNoDisponible as falla:
        raise _no_disponible() from falla


@router.post(
    "/{consentimientoId}/revocacion",
    response_model=ConsentimientoDto,
    response_model_by_alias=True,
    summary="Revoca un consentimiento del titular",
    responses={
        200: {"headers": CABECERAS},
        401: _NO_AUTENTICADO,
        403: error(_ROL),
        404: _NO_ENCONTRADO,
        422: _ID_INVALIDO,
        503: error(
            "SERVICIO_NO_DISPONIBLE: no se pudo confirmar la revocación; la app informa y puede "
            "repetirla: es idempotente por estado y no genera otro evento"
        ),
    },
)
async def revocar(
    consentimiento_id: ConsentimientoId, request: Request, identidad: Titular
) -> ConsentimientoDto:
    """Sin cuerpo ni Idempotency-Key: `clientes` es idempotente por estado (AC3). La primera vez
    y las repeticiones responden 200 con el mismo cuerpo; uno ya vencido responde 200 como
    `VENCIDO`, con `revocadoEn` nulo. El BFF pasa el estado tal como lo devuelve `clientes`."""
    caso: RevocarConsentimiento = request.app.state.revocar_consentimiento
    try:
        return ConsentimientoDto.desde(await caso.ejecutar(identidad.sub, consentimiento_id))
    except ConsentimientoNoEncontrado as falla:
        raise _no_encontrado() from falla
    except ServicioNoDisponible as falla:
        raise _no_disponible() from falla


@router.get(
    "/{consentimientoId}/historial",
    response_model=HistorialDto,
    response_model_by_alias=True,
    summary="Historial de un consentimiento del titular: otorgado y revocado o vencido",
    responses={
        200: {"headers": CABECERAS},
        401: _NO_AUTENTICADO,
        403: error(_ROL),
        404: _NO_ENCONTRADO,
        422: _ID_INVALIDO,
        503: error("SERVICIO_NO_DISPONIBLE: `clientes` no respondió"),
    },
)
async def historial(
    consentimiento_id: ConsentimientoId, request: Request, identidad: Titular
) -> HistorialDto:
    """Solo lectura (AC4): los hitos los deriva `clientes` de la fila inmutable."""
    consultas: ConsultarConsentimientos = request.app.state.consultar_consentimientos
    try:
        return HistorialDto.desde(await consultas.historial(identidad.sub, consentimiento_id))
    except ConsentimientoNoEncontrado as falla:
        raise _no_encontrado() from falla
    except ServicioNoDisponible as falla:
        raise _no_disponible() from falla
