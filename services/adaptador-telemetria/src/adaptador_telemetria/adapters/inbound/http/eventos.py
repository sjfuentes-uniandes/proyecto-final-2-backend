"""`POST /v1/eventos` de `adaptador-telemetria` (HU-W21, DS3-16).

La entrada pública es `POST /telemetria/v1/eventos` (API Gateway, HU-S03); llega al adaptador como
`POST /v1/eventos`, sin JWT y con el cuerpo intacto. Quien la llama es una fuente de telemetría
(un generador sintético; nunca un proveedor real) que firma cada envío (DS3-16):

    X-Telemetria-Firma = "sha256=" + hex(HMAC-SHA256(secreto, marca + "." + cuerpo crudo))

La ruta **lee el cuerpo crudo antes de parsearlo** (la firma se calcula sobre los bytes recibidos),
así que el cuerpo no se declara como parámetro: FastAPI lo validaría antes de la firma y un cuerpo
malformado con firma inválida respondería 422 en vez de 401. Por eso el esquema `EventoExterno` (del
dominio) se publica con `publicar_esquemas`, que lo agrega a `components.schemas` y a `requestBody`
(un `app.openapi()` con la forma del contrato, R-02 del Sprint 2).

Los encabezados se declaran opcionales: la ausencia de uno es un 401, no un 422 (no se revela qué
falló). `ErrorDto` y `_error` repiten el estilo de `adaptador-pagos` (`cobros.py`).
"""

from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, FastAPI, Header, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from solventa_common import correlation
from solventa_common.errors import ErrorNegocio

from adaptador_telemetria.application.telemetria.puertos import DependenciaNoDisponible
from adaptador_telemetria.application.telemetria.recibir_evento import RecibirEvento
from adaptador_telemetria.domain.telemetria.contrato import EventoExterno
from adaptador_telemetria.domain.telemetria.errores import (
    ContratoInvalido,
    FirmaInvalida,
    MarcaFueraDeRango,
)

router = APIRouter(prefix="/v1/eventos", tags=["eventos"])

FIRMA_INVALIDA = "FIRMA_INVALIDA"
MARCA_TIEMPO_FUERA_DE_RANGO = "MARCA_TIEMPO_FUERA_DE_RANGO"
CONTRATO_INVALIDO = "CONTRATO_INVALIDO"
DEPENDENCIA_NO_DISPONIBLE = "DEPENDENCIA_NO_DISPONIBLE"


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class RecepcionEvento(_Dto):
    """202 si el evento es nuevo; 200 si es un reenvío (el mismo `eventoId` y `recibidoEn`)."""

    evento_id: UUID = Field(description="UUIDv5 de (fuente, idExterno)")
    recibido_en: AwareDatetime


class ErrorDto(BaseModel):
    codigo: str
    mensaje: str
    correlationId: str | None
    detalles: dict[str, Any]


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


@router.post(
    "",
    status_code=202,
    response_model=RecepcionEvento,
    response_model_by_alias=True,
    responses={
        202: {
            "model": RecepcionEvento,
            "description": "Evento nuevo: archivado y publicado en el Outbox",
        },
        200: {
            "model": RecepcionEvento,
            "description": "Reenvío de un evento ya recibido: mismo `eventoId`, sin otra fila "
            "en el Outbox",
        },
        401: _error(
            "FIRMA_INVALIDA: firma ausente o inválida, o fuente fuera de la lista configurada. "
            "MARCA_TIEMPO_FUERA_DE_RANGO: la marca de tiempo se aparta más de 5 minutos "
            "(en cualquier sentido) del reloj del servicio. La firma se verifica primero"
        ),
        422: _error(
            "CONTRATO_INVALIDO: el cuerpo no es JSON, no cumple `EventoExterno` o su "
            "`versionContrato` no está soportada. `detalles.campos` lista los campos inválidos, "
            "nunca sus valores"
        ),
        503: _error(
            "DEPENDENCIA_NO_DISPONIBLE: no se pudo archivar el original ni registrar el evento; "
            "la fuente puede reenviar el mismo evento (es idempotente)"
        ),
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/EventoExterno"}}
            },
        }
    },
)
async def recibir(
    request: Request,
    response: Response,
    fuente: Annotated[
        str | None,
        Header(
            alias="X-Telemetria-Fuente",
            description="Fuente de la telemetría; debe estar en `TELEMETRIA_FUENTES`",
            examples=["IDEAM-SIM"],
        ),
    ] = None,
    marca_tiempo: Annotated[
        str | None,
        Header(
            alias="X-Telemetria-Marca-Tiempo",
            description="Instante UTC ISO 8601 con Z en que la fuente firmó el envío; entra tal "
            "cual en la firma",
            examples=["2026-11-10T14:00:05Z"],
        ),
    ] = None,
    firma: Annotated[
        str | None,
        Header(
            alias="X-Telemetria-Firma",
            description="`sha256=<hex>` de HMAC-SHA256 sobre `<marca-tiempo>.<cuerpo crudo>` con "
            "el secreto del aliado `telemetria`",
        ),
    ] = None,
) -> RecepcionEvento:
    """Valida y publica un evento externo firmado (HU-W21, DS3-16).

    Orden: (1) fuente y firma, comparada en tiempo constante sobre el cuerpo crudo; (2) desfase de
    la marca de tiempo (± 300 s); (3) contrato; (4) archivo del original en
    `telemetria/<AAAA>/<MM>/<DD>/<eventoId>.json`; (5) en una transacción, el registro del evento
    y su `EventoParametricoObservado` en el Outbox. Un rechazo (401 o 422) no archiva ni escribe
    nada. Sin JWT: la autenticidad es la firma.
    """
    caso: RecibirEvento = request.app.state.recibir_evento
    # Los bytes tal como llegaron: la firma se calcula sobre ellos.
    cuerpo = await request.body()
    try:
        recepcion = await caso(
            fuente, marca_tiempo, firma, cuerpo, correlation.obtener() or str(uuid4())
        )
    except FirmaInvalida:
        raise ErrorNegocio(FIRMA_INVALIDA, "La firma no es válida", 401) from None
    except MarcaFueraDeRango:
        raise ErrorNegocio(
            MARCA_TIEMPO_FUERA_DE_RANGO, "La marca de tiempo está fuera del rango permitido", 401
        ) from None
    except ContratoInvalido as error:
        raise ErrorNegocio(
            CONTRATO_INVALIDO,
            "El evento no cumple el contrato",
            422,
            {"campos": list(error.campos)},
        ) from None
    except DependenciaNoDisponible:
        raise ErrorNegocio(
            DEPENDENCIA_NO_DISPONIBLE, "No se pudo procesar el evento; reenvíelo", 503
        ) from None
    if recepcion.duplicado:
        response.status_code = 200
    return RecepcionEvento(evento_id=recepcion.evento_id, recibido_en=recepcion.recibido_en)


def publicar_esquemas(app: FastAPI) -> None:
    """Agrega `EventoExterno` (y `Ubicacion`) a `components.schemas` de `app.openapi()`."""
    original = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        esquema = original()
        modelo = EventoExterno.model_json_schema(ref_template="#/components/schemas/{model}")
        componentes = esquema.setdefault("components", {}).setdefault("schemas", {})
        for nombre, definicion in modelo.pop("$defs", {}).items():
            componentes[nombre] = definicion
        componentes["EventoExterno"] = modelo
        return esquema

    app.openapi = openapi  # type: ignore[method-assign]
