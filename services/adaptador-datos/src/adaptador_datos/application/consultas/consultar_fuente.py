"""Caso de uso: consultar una fuente autorizada y devolverla en el modelo canónico (03, W10-P04)."""

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from uuid import UUID

from solventa_common.resiliencia import DependenciaNoDisponible, Proteccion, TiempoAgotado
from solventa_common.telemetry import RESULTADO_RECHAZADO, RESULTADO_TIMEOUT, operacion

from adaptador_datos.application.consultas.puertos import PuertoProveedor
from adaptador_datos.domain.consultas.catalogo_fuentes import (
    CATALOGO,
    PROVEEDOR,
    claves_proveedor,
)
from adaptador_datos.domain.consultas.modelos import (
    Aliado,
    CamposNoSoportados,
    ConsentimientoRequerido,
    Fuente,
    RespuestaConsulta,
    RespuestaInvalida,
)
from adaptador_datos.domain.consultas.proyeccion import proyectar

logger = logging.getLogger("adaptador_datos.consultas")


def _ahora() -> datetime:
    return datetime.now(UTC)


class ConsultarFuente:
    def __init__(
        self,
        proveedores: Mapping[Aliado, PuertoProveedor],
        proteccion: Proteccion,
        timeouts_ms: Mapping[Aliado, int],
        reloj: Callable[[], datetime] = _ahora,
    ) -> None:
        self._proveedores = proveedores
        self._proteccion = proteccion
        self._timeouts_ms = timeouts_ms
        self._reloj = reloj

    async def ejecutar(
        self,
        fuente: Fuente,
        cliente_id: UUID,
        consentimiento_id: UUID | None,
        campos: Sequence[str],
        deadline_ms: int | None,
    ) -> RespuestaConsulta:
        definicion = CATALOGO[fuente]
        with operacion(f"consulta.{fuente}") as op:
            if consentimiento_id is None:
                op.marcar(RESULTADO_RECHAZADO)
                raise ConsentimientoRequerido()
            invalidos = tuple(c for c in campos if c not in definicion.campos)
            if not campos or invalidos:
                op.marcar(RESULTADO_RECHAZADO)
                raise CamposNoSoportados(invalidos)
            proveedor = self._proveedores[definicion.aliado]
            claves = claves_proveedor(fuente, campos)

            async def llamada(timeout_s: float) -> object:
                return await proveedor.obtener(fuente, cliente_id, claves, timeout_s)

            try:
                cuerpo = await self._proteccion.ejecutar(
                    clave=fuente,
                    grupo=definicion.aliado,
                    llamada=llamada,
                    configurado_ms=self._timeouts_ms[definicion.aliado],
                    deadline_ms=deadline_ms,
                )
            except TiempoAgotado:
                op.marcar(RESULTADO_TIMEOUT)
                raise
            except DependenciaNoDisponible:
                op.marcar("error")
                raise
            try:
                valores, descartados, version, confianza = proyectar(fuente, campos, cuerpo)
            except RespuestaInvalida:
                op.marcar("error")
                raise
            # Solo metadatos: nunca el cuerpo del proveedor ni los valores (regla 10).
            logger.info(
                "consulta procesada",
                extra={"fuente": str(fuente), "campos_descartados": descartados},
            )
            respuesta = RespuestaConsulta(
                fuente=fuente,
                tipo=definicion.tipo,
                proveedor=PROVEEDOR[definicion.aliado],
                version_fuente=version,
                confianza=confianza,
                consultada_en=self._reloj(),
                campos=valores,
                campos_descartados=descartados,
            )
        return respuesta
