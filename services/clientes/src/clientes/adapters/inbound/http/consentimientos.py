"""API del consentimiento para el uso de datos (HU-M08): otorgar, listar, detalle y texto vigente.

Las rutas `actual` resuelven el cliente por el `sub` del titular (ADR 0006); un consentimiento
ajeno responde como uno inexistente (404). Solo `bff-movil` invoca estas rutas.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Path, Query, Request, Response
from pydantic import Field
from solventa_common import correlation
from solventa_common.errors import ErrorNegocio

from clientes.adapters.inbound.http.comun import ERRORES_ACTOR, Dto, Titular, error
from clientes.application.consentimientos.casos import (
    ConsultarConsentimientos,
    ConsultarTextoVigente,
    OtorgarConsentimiento,
)
from clientes.domain.consentimientos.modelos import (
    Consentimiento,
    ConsentimientoNoEncontrado,
    ConsentimientoYaVigente,
    EstadoConsentimiento,
    IdempotenciaConflicto,
    IdentidadNoVerificada,
    SolicitudInvalida,
    TextoNoVigente,
    VigenciaInvalida,
)
from clientes.domain.consentimientos.textos import TextoLegal
from clientes.domain.identidad.modelos import ClienteNoEncontrado

router = APIRouter(prefix="/v1/clientes/actual/consentimientos", tags=["consentimientos"])
router_textos = APIRouter(prefix="/v1/consentimientos/textos", tags=["consentimientos"])

IDEMPOTENT_REPLAYED = "Idempotent-Replayed"


class SolicitudOtorgar(Dto):
    """Todos los campos son opcionales para el esquema: la validación del dominio informa en
    `detalles.campos` todos los que faltan o son inválidos, no solo el primero (AC3)."""

    proposito: Annotated[str | None, Field(max_length=64)] = None
    alcances: Annotated[list[Annotated[str, Field(max_length=64)]] | None, Field(max_length=16)] = (
        None
    )
    vigencia_meses: int | None = None
    version_texto: Annotated[str | None, Field(max_length=64)] = None
    idioma: Annotated[str | None, Field(max_length=16)] = None


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
            consentimiento_id=consentimiento.id,
            estado=consentimiento.estado_derivado,
            proposito=consentimiento.proposito,
            alcances=list(consentimiento.alcances),
            version_texto=consentimiento.version_texto,
            idioma=consentimiento.idioma,
            otorgado_en=consentimiento.otorgado_en,
            vigente_hasta=consentimiento.vigente_hasta,
            revocado_en=consentimiento.revocado_en,
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
    def desde(cls, texto: TextoLegal) -> "TextoVigenteDto":
        return cls(
            proposito=texto.proposito,
            idioma=texto.idioma,
            version_texto=texto.version,
            titulo=texto.titulo,
            cuerpo=texto.cuerpo,
            aviso=texto.aviso,
            vigencias_meses=list(texto.vigencias_meses),
            vigencia_defecto_meses=texto.vigencia_defecto_meses,
        )


ConsentimientoId = Annotated[UUID, Path(alias="consentimientoId")]


@router.post(
    "",
    status_code=201,
    response_model=ConsentimientoDto,
    response_model_by_alias=True,
    summary="Otorga el consentimiento para consultar datos financieros",
    responses={
        200: {
            "model": ConsentimientoDto,
            "description": "Repetición: misma Idempotency-Key y mismo cuerpo. Devuelve la "
            "representación actual del consentimiento",
            "headers": {
                IDEMPOTENT_REPLAYED: {"description": "Siempre `true`", "schema": {"type": "string"}}
            },
        },
        **ERRORES_ACTOR,
        400: error("ERROR_HTTP: el cuerpo no se puede leer como JSON"),
        403: error(
            "ROL_NO_AUTORIZADO: X-Actor-Rol distinto de CLIENTE; o IDENTIDAD_NO_VERIFICADA: "
            "la identidad del titular no está VERIFICADA"
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
    },
)
async def otorgar(
    solicitud: SolicitudOtorgar,
    request: Request,
    respuesta: Response,
    sub: Titular,
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> ConsentimientoDto:
    """Crea el consentimiento `ACTIVO`; el servidor calcula `vigenteHasta` (ADR 0005)."""
    caso: OtorgarConsentimiento = request.app.state.otorgar_consentimiento
    try:
        resultado = await caso.ejecutar(
            sub,
            idempotency_key,
            proposito=solicitud.proposito,
            alcances=solicitud.alcances,
            vigencia_meses=solicitud.vigencia_meses,
            version_texto=solicitud.version_texto,
            idioma=solicitud.idioma,
            correlation_id=correlation.obtener() or "",
        )
    except SolicitudInvalida as falla:
        raise ErrorNegocio(
            "SOLICITUD_INVALIDA", "Solicitud inválida", 422, {"campos": list(falla.campos)}
        ) from falla
    except VigenciaInvalida as falla:
        raise ErrorNegocio("VIGENCIA_INVALIDA", "La vigencia no está permitida", 422) from falla
    except TextoNoVigente as falla:
        raise ErrorNegocio("TEXTO_NO_VIGENTE", "El texto no es el vigente", 422) from falla
    except IdempotenciaConflicto as falla:
        raise ErrorNegocio(
            "IDEMPOTENCIA_CONFLICTO", "La clave ya se usó con otro cuerpo", 422
        ) from falla
    except IdentidadNoVerificada as falla:
        raise ErrorNegocio(
            "IDENTIDAD_NO_VERIFICADA", "La identidad no está verificada", 403
        ) from falla
    except ConsentimientoYaVigente as falla:
        raise ErrorNegocio(
            "CONSENTIMIENTO_YA_VIGENTE",
            "Ya hay un consentimiento vigente",
            409,
            {"consentimientoId": str(falla.consentimiento_id)},
        ) from falla
    except ClienteNoEncontrado as falla:
        raise ErrorNegocio("CLIENTE_NO_ENCONTRADO", "El cliente no existe", 404) from falla
    if resultado.repeticion:
        respuesta.status_code = 200
        respuesta.headers[IDEMPOTENT_REPLAYED] = "true"
    return ConsentimientoDto.desde(resultado.consentimiento)


@router.get(
    "",
    response_model=list[ConsentimientoDto],
    response_model_by_alias=True,
    summary="Consentimientos del titular, del más reciente al más antiguo",
    responses={**ERRORES_ACTOR},
)
async def listar(request: Request, sub: Titular) -> list[ConsentimientoDto]:
    """El estado es el derivado al leer (`VENCIDO` si ya pasó `vigenteHasta`). Quien aún no
    tiene cliente recibe una lista vacía."""
    consultas: ConsultarConsentimientos = request.app.state.consultar_consentimientos
    return [ConsentimientoDto.desde(c) for c in await consultas.listar(sub)]


@router.get(
    "/{consentimientoId}",
    response_model=ConsentimientoDto,
    response_model_by_alias=True,
    summary="Detalle y comprobante de un consentimiento del titular",
    responses={
        **ERRORES_ACTOR,
        404: error(
            "CONSENTIMIENTO_NO_ENCONTRADO: no existe o es de otro cliente (no se distingue)"
        ),
    },
)
async def detalle(
    consentimiento_id: ConsentimientoId, request: Request, sub: Titular
) -> ConsentimientoDto:
    consultas: ConsultarConsentimientos = request.app.state.consultar_consentimientos
    try:
        return ConsentimientoDto.desde(await consultas.detalle(sub, consentimiento_id))
    except ConsentimientoNoEncontrado as falla:
        raise ErrorNegocio(
            "CONSENTIMIENTO_NO_ENCONTRADO", "El consentimiento no existe", 404
        ) from falla


@router_textos.get(
    "/vigente",
    response_model=TextoVigenteDto,
    response_model_by_alias=True,
    summary="Texto de ejemplo vigente, su versión y las vigencias permitidas",
    responses={422: error("SOLICITUD_INVALIDA: proposito o idioma ausentes o no soportados")},
)
async def texto_vigente(
    request: Request,
    proposito: Annotated[str | None, Query(max_length=64)] = None,
    idioma: Annotated[str | None, Query(max_length=16)] = None,
) -> TextoVigenteDto:
    """Texto ilustrativo, sin validez legal (ADR 0005). No contiene datos del titular."""
    caso: ConsultarTextoVigente = request.app.state.consultar_texto_vigente
    try:
        return TextoVigenteDto.desde(caso.ejecutar(proposito, idioma))
    except SolicitudInvalida as falla:
        raise ErrorNegocio(
            "SOLICITUD_INVALIDA", "Solicitud inválida", 422, {"campos": list(falla.campos)}
        ) from falla
