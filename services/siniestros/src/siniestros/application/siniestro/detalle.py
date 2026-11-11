"""Detalle del siniestro (HU-W19: H03, H04, cobertura e historial).

1. Lee el caso en una transacción corta y la cierra.
2. Verifica la integridad de cada objeto con `HeadObject` **en paralelo y sin conexión de base
   abierta**: cada llamada tiene su corte; un objeto ausente no impide la respuesta (AC4).
3. Calcula la cobertura, la coherencia y el trámite con el dominio (puro).
4. Escribe `SiniestroConsultado` en el Outbox en otra transacción corta.
"""

import asyncio
from uuid import UUID

from solventa_common.telemetry import RESULTADO_RECHAZADO, operacion

from siniestros.application.siniestro.decidir import ACCION_SOPORTE_SOLICITADO
from siniestros.application.siniestro.errores import SiniestroNoEncontrado
from siniestros.application.siniestro.puertos import (
    AlmacenEvidencias,
    Reloj,
    SiniestrosRepositorio,
    SoportesRequeridos,
)
from siniestros.application.siniestro.vistas import (
    Actor,
    DetalleCaso,
    EvidenciaVerificada,
    Plazos,
    resumen_de,
)
from siniestros.domain.siniestro.cobertura import soportes_pendientes, verificar_cobertura
from siniestros.domain.siniestro.coherencia import DISTANCIA_MAX_M, Declarado, evaluar_coherencia
from siniestros.domain.siniestro.modelos import (
    EntradaHistorial,
    EstadoSiniestro,
    EventoReclamado,
    Siniestro,
    TipoSoporte,
)
from siniestros.domain.siniestro.pago import pago_de
from siniestros.domain.siniestro.tramite import pasos_tramite


def soportes_solicitados(
    caso: Siniestro, historial: tuple[EntradaHistorial, ...]
) -> tuple[TipoSoporte, ...]:
    """Los documentos que el analista pidió en el último pedido de soporte, mientras el caso espera
    a que el cliente responda. Pasado el plazo (o sin pedido) no hay ninguno."""
    if caso.estado is not EstadoSiniestro.FALTA_SOPORTE:
        return ()
    pedidos = [h for h in historial if h.accion == ACCION_SOPORTE_SOLICITADO and h.detalles]
    if not pedidos:
        return ()
    ultimo = max(pedidos, key=lambda h: h.ocurrido_en)
    assert ultimo.detalles is not None
    return tuple(TipoSoporte(d) for d in ultimo.detalles.get("documentos", ()))


class ConsultarDetalle:
    def __init__(
        self,
        repo: SiniestrosRepositorio,
        almacen: AlmacenEvidencias,
        soportes: SoportesRequeridos,
        reloj: Reloj,
        plazos: Plazos,
        distancia_max_m: int = DISTANCIA_MAX_M,
    ) -> None:
        self._repo = repo
        self._almacen = almacen
        self._soportes = soportes
        self._reloj = reloj
        self._plazos = plazos
        self._distancia_max_m = distancia_max_m

    async def ejecutar(self, siniestro_id: UUID, actor: Actor) -> DetalleCaso:
        with operacion("siniestro.consultar") as op:
            leido = await self._repo.detalle(siniestro_id)
            if leido is None:
                op.marcar(RESULTADO_RECHAZADO)
                raise SiniestroNoEncontrado

            caso, evidencias = leido.siniestro, leido.evidencias
            resultados = await asyncio.gather(
                *(self._almacen.verificar(e.clave_objeto, e.sha256) for e in evidencias)
            )
            integridades = {e.id: i for e, i in zip(evidencias, resultados, strict=True)}

            ahora = self._reloj.ahora()
            requeridos = self._soportes.para(caso.causa)
            evento = EventoReclamado(caso.causa, caso.fecha_evento, caso.reportado_en)
            detalle = DetalleCaso(
                resumen=resumen_de(caso, ahora, self._plazos),
                verificacion_cobertura=verificar_cobertura(
                    caso.copia_poliza, evento, evidencias, requeridos
                ),
                coherencia=evaluar_coherencia(
                    Declarado(caso.fecha_evento, caso.ubicacion_declarada),
                    evidencias,
                    integridades,
                    distancia_max_m=self._distancia_max_m,
                ),
                evidencias=tuple(EvidenciaVerificada(e, integridades[e.id]) for e in evidencias),
                soportes_pendientes=soportes_pendientes(
                    requeridos, evidencias, soportes_solicitados(caso, leido.historial)
                ),
                tramite=pasos_tramite(caso.estado, leido.historial),
                historial=leido.historial,
                liquidacion=leido.liquidacion,
                pago=pago_de(caso.estado, caso.codigo, leido.pago),
            )

            async with self._repo.transaccion() as tx:
                await tx.evento_consultado(caso, actor.id, ahora)
            return detalle
