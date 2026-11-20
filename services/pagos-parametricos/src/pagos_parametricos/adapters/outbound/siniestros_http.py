"""`Siniestros` sobre `POST /v1/siniestros/parametricos` (HU-W22, DS3-23, W22-B08): la ACL hacia
`siniestros`.

- `Idempotency-Key = <eventoId>:<polizaId>`, `X-Actor-Id = pagos-parametricos` y `X-Actor-Rol =
  SISTEMA_PARAMETRICO`; `X-Correlation-Id` lo propaga `correlation.cliente_async`.
- Protegido con `ProteccionDependencia`: timeout (600 ms, corte duro de 700, §13 regla 7), un
  reintento acotado solo para conexión o 5xx, circuito por operación y *bulkhead* de 10.
- Solo `201` (lo creó) y `200` (ya existía) con un `ResumenSiniestro` cuyo `id` y `codigo` leemos
  son respuestas definitivas. Un 5xx cuenta como fallo del circuito y se traduce a
  `DependenciaNoDisponible`; un 4xx o un cuerpo fuera de contrato es `SiniestrosRechazo` (con el
  estado y el `codigo` del error, nunca el cuerpo).
- Nunca registra la solicitud ni la respuesta: la medición, el cliente y el valor no salen de aquí.
"""

from typing import Annotated, Any
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel
from solventa_common.resiliencia import Proteccion

from pagos_parametricos.domain.parametrico.activacion import SiniestroCreado, SolicitudSiniestro
from pagos_parametricos.domain.parametrico.errores import SiniestrosRechazo

RUTA = "/v1/siniestros/parametricos"
CLAVE_OPERACION = "siniestros.crear_parametrico"
GRUPO = "siniestros"
ACTOR = "pagos-parametricos"
ROL = "SISTEMA_PARAMETRICO"
CREADO = 201
EXISTENTE = 200


class _ResumenLeido(BaseModel):
    """Lo único que `pagos-parametricos` lee del `ResumenSiniestro` (lector tolerante)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

    id: UUID
    codigo: Annotated[str, Field(pattern=r"^88-[0-9]{4}-[0-9]{4,}$")]


class _Error(BaseModel):
    model_config = ConfigDict(extra="ignore")

    codigo: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{2,63}$")]


def cuerpo_de(solicitud: SolicitudSiniestro) -> dict[str, Any]:
    """El cuerpo del contrato (`SolicitudSiniestroParametrico`). Los montos, como texto decimal."""
    e = solicitud.evento
    return {
        "eventoId": str(solicitud.evento_id),
        "polizaId": str(solicitud.poliza_id),
        "clienteId": str(solicitud.cliente_id),
        "poliza": {
            "numero": solicitud.numero,
            "ramo": solicitud.ramo,
            "vigencia": {
                "desde": solicitud.vigencia_desde.isoformat(),
                "hasta": solicitud.vigencia_hasta.isoformat(),
            },
            "moneda": solicitud.moneda,
        },
        "evento": {
            "fuente": e.fuente,
            "tipoEvento": e.tipo_evento,
            "valorObservado": format(e.valor_observado, "f"),
            "umbral": solicitud.umbral,
            "unidad": e.unidad,
            "zona": e.zona,
            "observadoEn": e.observado_en.isoformat().replace("+00:00", "Z"),
            "eventoOrigen": e.evento_origen,
        },
        "regla": solicitud.regla,
        "versionRegla": solicitud.version_regla,
        "valor": solicitud.valor,
        "moneda": solicitud.moneda,
    }


def _codigo_de(respuesta: httpx.Response) -> str | None:
    try:
        return _Error.model_validate_json(respuesta.content).codigo
    except ValidationError:
        return None


class SiniestrosHttp:
    """Implementa `Siniestros`. El cliente HTTP lo crea la app con `correlation.cliente_async()`."""

    def __init__(
        self, cliente: httpx.AsyncClient, base_url: str, timeout_ms: int, proteccion: Proteccion
    ) -> None:
        self._cliente = cliente
        self._base_url = base_url.rstrip("/")
        self._timeout_ms = timeout_ms
        self._proteccion = proteccion

    async def crear(self, solicitud: SolicitudSiniestro) -> SiniestroCreado:
        cuerpo = cuerpo_de(solicitud)
        encabezados = {
            "Idempotency-Key": solicitud.clave,
            "X-Actor-Id": ACTOR,
            "X-Actor-Rol": ROL,
        }

        async def llamada(timeout_s: float) -> httpx.Response:
            respuesta = await self._cliente.post(
                f"{self._base_url}{RUTA}", json=cuerpo, headers=encabezados, timeout=timeout_s
            )
            if respuesta.status_code >= 500:
                respuesta.raise_for_status()  # cuenta como fallo del circuito y es reintentable
            return respuesta

        respuesta = await self._proteccion.ejecutar(
            CLAVE_OPERACION, GRUPO, llamada, self._timeout_ms, None
        )
        if respuesta.status_code not in (CREADO, EXISTENTE):
            raise SiniestrosRechazo(respuesta.status_code, _codigo_de(respuesta))
        try:
            leido = _ResumenLeido.model_validate_json(respuesta.content)
        except ValidationError:
            raise SiniestrosRechazo(respuesta.status_code, None) from None
        return SiniestroCreado(leido.id, leido.codigo, respuesta.status_code == CREADO)
