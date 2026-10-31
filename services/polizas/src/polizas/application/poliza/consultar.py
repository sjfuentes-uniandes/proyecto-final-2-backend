"""La lectura de la póliza (HU-W17, 17.9; ADR 0014, decisiones 4 y 5): una operación medida por
ruta. Sin llamadas a otros servicios; solo la URL del documento se firma con el almacén.

- `ConsultarPoliza` (`poliza.consultar`): la póliza de una cotización, con la oferta aceptada y el
  recaudo. Sin aceptación, `AceptacionNoEncontrada`; con aceptación y sin póliza (recaudo pendiente
  o rechazado, o producto sin ramo), `PolizaNoEncontrada` (AC2).
- `DescargarDocumento` (`poliza.documento`): la URL prefirmada de la **versión registrada** del PDF,
  solo de una póliza ACTIVA. `vence_en` se toma antes de firmarla: nunca es posterior al
  vencimiento real. La URL no va a los logs.
- `VerificarPoliza` (`poliza.verificar`): número + huella contra lo firmado (decisión 5). Ni la
  medición ni los logs distinguen por qué una verificación no es válida.
"""

import logging
from datetime import timedelta
from uuid import UUID

from solventa_common.telemetry import RESULTADO_RECHAZADO, operacion

from polizas.application.aceptacion.puertos import AceptacionesRepositorio
from polizas.application.poliza.puertos import AlmacenDocumentos, LecturaPolizas
from polizas.application.recaudo.puertos import RecaudosRepositorio
from polizas.application.suscripcion.puertos import DecisionesRepositorio, Reloj
from polizas.domain.aceptacion.errores import AceptacionNoEncontrada
from polizas.domain.poliza.consulta import (
    EnlaceDocumento,
    Verificacion,
    VistaPoliza,
    verificar,
    vista_poliza,
)
from polizas.domain.poliza.errores import (
    FalloAlmacenDocumentos,
    PolizaNoActiva,
    PolizaNoEncontrada,
)
from polizas.domain.poliza.modelos import EstadoPoliza
from polizas.domain.recaudo.modelos import SituacionRecaudo
from polizas.domain.suscripcion.errores import DependenciaNoDisponible

OPERACION_CONSULTAR = "poliza.consultar"
OPERACION_DOCUMENTO = "poliza.documento"
OPERACION_VERIFICAR = "poliza.verificar"
RESULTADO_VALIDA = "valida"
RESULTADO_NO_VALIDA = "no_valida"
# `detalles.dependencia` del 503: el bucket de los documentos (sin configurar o sin responder).
DEPENDENCIA_DOCUMENTOS = "documentos"
MENSAJE_URL_NO_FIRMADA = "poliza: no se pudo firmar la URL del documento"

logger = logging.getLogger("polizas.poliza")


class ConsultarPoliza:
    def __init__(
        self,
        aceptaciones: AceptacionesRepositorio,
        decisiones: DecisionesRepositorio,
        recaudos: RecaudosRepositorio,
        polizas: LecturaPolizas,
    ) -> None:
        self._aceptaciones = aceptaciones
        self._decisiones = decisiones
        self._recaudos = recaudos
        self._polizas = polizas

    async def por_cotizacion(self, cotizacion_id: UUID) -> VistaPoliza:
        with operacion(OPERACION_CONSULTAR) as op:
            aceptacion = await self._aceptaciones.por_cotizacion(cotizacion_id)
            if aceptacion is None:
                op.marcar(RESULTADO_RECHAZADO)
                raise AceptacionNoEncontrada()
            poliza = await self._polizas.por_aceptacion(aceptacion.id)
            if poliza is None:
                op.marcar(RESULTADO_RECHAZADO)
                raise PolizaNoEncontrada()
            decision = await self._decisiones.por_id(aceptacion.decision_id)
            if decision is None:
                # No pasa: la aceptación tiene llave hacia su decisión y las dos son inmutables.
                raise DependenciaNoDisponible("polizas.decisiones")
            recaudo = await self._recaudos.por_clave(aceptacion.id)
            if recaudo is None or recaudo.estado is not SituacionRecaudo.CONFIRMADO:
                # No pasa: la póliza nace con el RecaudoConfirmado, en la misma transacción.
                raise DependenciaNoDisponible("polizas.recaudos")
            return vista_poliza(poliza, aceptacion, decision.insumos, recaudo)


class DescargarDocumento:
    def __init__(
        self,
        polizas: LecturaPolizas,
        almacen: AlmacenDocumentos | None,
        reloj: Reloj,
        vence_s: int,
    ) -> None:
        self._polizas = polizas
        self._almacen = almacen
        self._reloj = reloj
        self._vence_s = vence_s

    async def ejecutar(self, poliza_id: UUID) -> EnlaceDocumento:
        """Lanza PolizaNoEncontrada, PolizaNoActiva (EMITIENDO, aunque el documento ya esté
        subido) o DependenciaNoDisponible (sin almacén, o si no firma la URL)."""
        with operacion(OPERACION_DOCUMENTO) as op:
            poliza = await self._polizas.por_id(poliza_id)
            if poliza is None:
                op.marcar(RESULTADO_RECHAZADO)
                raise PolizaNoEncontrada()
            if poliza.estado is not EstadoPoliza.ACTIVA or poliza.documento is None:
                op.marcar(RESULTADO_RECHAZADO)
                raise PolizaNoActiva()
            if self._almacen is None:
                raise DependenciaNoDisponible(DEPENDENCIA_DOCUMENTOS)
            vence_en = self._reloj.ahora() + timedelta(seconds=self._vence_s)
            documento = poliza.documento
            try:
                url = await self._almacen.url_descarga(
                    documento.clave, documento.version_id, self._vence_s
                )
            except FalloAlmacenDocumentos as error:
                logger.warning(
                    MENSAJE_URL_NO_FIRMADA,
                    extra={
                        "poliza_id": str(poliza.poliza_id),
                        "codigo": error.codigo,
                        "error_s3": error.error_s3,
                    },
                )
                raise DependenciaNoDisponible(DEPENDENCIA_DOCUMENTOS) from error
            return EnlaceDocumento(url=url, vence_en=vence_en)


class VerificarPoliza:
    def __init__(self, polizas: LecturaPolizas) -> None:
        self._polizas = polizas

    async def ejecutar(self, numero: str, huella: str) -> Verificacion:
        with operacion(OPERACION_VERIFICAR) as op:
            verificacion = verificar(await self._polizas.por_numero(numero), numero, huella)
            op.marcar(RESULTADO_VALIDA if verificacion.valido else RESULTADO_NO_VALIDA)
            return verificacion
