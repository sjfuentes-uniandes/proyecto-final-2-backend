from uuid import UUID

from solventa_common.telemetry import (
    RESULTADO_ERROR,
    RESULTADO_RECHAZADO,
    RESULTADO_TIMEOUT,
    operacion,
)

from bff_movil.application.consentimientos.puertos import ServicioConsentimientos
from bff_movil.domain.consentimientos.modelos import (
    Consentimiento,
    ConsentimientoNoEncontrado,
    ConsentimientoYaVigente,
    IdentidadNoVerificada,
    Otorgamiento,
    SolicitudOtorgamiento,
    SolicitudRechazada,
    TextoVigente,
)
from bff_movil.domain.sesion.errores import ClienteNoEncontrado, ServicioNoDisponible

_RECHAZOS = (
    IdentidadNoVerificada,
    ClienteNoEncontrado,
    ConsentimientoNoEncontrado,
    ConsentimientoYaVigente,
    SolicitudRechazada,
)


def _resultado(error: ServicioNoDisponible) -> str:
    return RESULTADO_TIMEOUT if error.motivo == "TIEMPO_AGOTADO" else RESULTADO_ERROR


class OtorgarConsentimiento:
    def __init__(self, servicio: ServicioConsentimientos) -> None:
        self._servicio = servicio

    async def ejecutar(
        self, sub: str, clave: UUID, solicitud: SolicitudOtorgamiento
    ) -> Otorgamiento:
        """Nunca reintenta: la app repite con la misma clave y `clientes` devuelve el mismo
        consentimiento (AC4), también si la primera respuesta se perdió por un timeout."""
        with operacion("consentimientos.otorgar") as op:
            try:
                return await self._servicio.otorgar(sub, clave, solicitud)
            except ServicioNoDisponible as error:
                op.marcar(_resultado(error))
                raise
            except _RECHAZOS:
                op.marcar(RESULTADO_RECHAZADO)
                raise


class ConsultarConsentimientos:
    def __init__(self, servicio: ServicioConsentimientos) -> None:
        self._servicio = servicio

    async def listar(self, sub: str) -> list[Consentimiento]:
        with operacion("consentimientos.listar") as op:
            try:
                return await self._servicio.listar(sub)
            except ServicioNoDisponible as error:
                op.marcar(_resultado(error))
                raise

    async def detalle(self, sub: str, consentimiento_id: UUID) -> Consentimiento:
        with operacion("consentimientos.detalle") as op:
            try:
                return await self._servicio.detalle(sub, consentimiento_id)
            except ServicioNoDisponible as error:
                op.marcar(_resultado(error))
                raise
            except ConsentimientoNoEncontrado:
                op.marcar(RESULTADO_RECHAZADO)
                raise


class ConsultarTextoVigente:
    def __init__(self, servicio: ServicioConsentimientos) -> None:
        self._servicio = servicio

    async def ejecutar(self, proposito: str | None, idioma: str | None) -> TextoVigente:
        with operacion("consentimientos.texto_vigente") as op:
            try:
                return await self._servicio.texto_vigente(proposito, idioma)
            except ServicioNoDisponible as error:
                op.marcar(_resultado(error))
                raise
            except SolicitudRechazada:
                op.marcar(RESULTADO_RECHAZADO)
                raise
