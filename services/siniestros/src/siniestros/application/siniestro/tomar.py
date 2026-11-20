"""Tomar el siguiente de la cola (HU-W19, DS3-03).

Una transacción corta (sin llamadas a otros servicios): bloquea el REPORTADO más antiguo sin analista
(`FOR UPDATE SKIP LOCKED`, así dos analistas nunca toman el mismo caso), lo asigna, escribe el
historial y `SiniestroEnEvaluacion` en el Outbox, y guarda la respuesta de la `Idempotency-Key`.
"""

import hashlib
from uuid import UUID, uuid4

from solventa_common.telemetry import operacion

from siniestros.application.siniestro.errores import ColaSinCasos, IdempotenciaConflicto
from siniestros.application.siniestro.puertos import Reloj, SiniestrosRepositorio
from siniestros.application.siniestro.vistas import Actor, Plazos, ResumenCaso, resumen_de
from siniestros.domain.siniestro.modelos import EntradaHistorial, EstadoSiniestro
from siniestros.domain.siniestro.tramite import ACCION_ASIGNADO
from siniestros.domain.siniestro.transiciones import asignar

_OPERACION = "POST /v1/siniestros/siguiente"
RESULTADO_ASIGNADO = "asignado"
RESULTADO_COLA_VACIA = "cola_vacia"


def huella_de(analista_id: UUID) -> str:
    """Identifica la solicitud: la misma clave de otro analista es un conflicto."""
    return hashlib.sha256(f"{_OPERACION}|{analista_id}".encode()).hexdigest()


class TomarSiguiente:
    def __init__(self, repo: SiniestrosRepositorio, reloj: Reloj, plazos: Plazos) -> None:
        self._repo = repo
        self._reloj = reloj
        self._plazos = plazos

    async def ejecutar(self, actor: Actor, analista_id: UUID, clave: UUID) -> ResumenCaso:
        with operacion("siniestro.tomar") as op:
            ahora = self._reloj.ahora()
            clave_guardada = f"siguiente:{clave}"
            huella = huella_de(analista_id)
            async with self._repo.transaccion() as tx:
                previo = await tx.adquirir_idempotencia(clave_guardada)
                if previo is not None:
                    if previo.huella != huella:
                        raise IdempotenciaConflicto
                    repetido = await tx.siniestro(UUID(previo.respuesta["siniestroId"]))
                    assert repetido is not None  # el siniestro no se borra
                    op.marcar(RESULTADO_ASIGNADO)
                    return resumen_de(repetido, ahora, self._plazos)

                libre = await tx.reservar_siguiente()
                if libre is None:
                    op.marcar(RESULTADO_COLA_VACIA)
                    raise ColaSinCasos
                asignado = asignar(libre, analista_id, ahora)
                await tx.guardar_asignacion(asignado, libre.version)
                await tx.agregar_historial(
                    EntradaHistorial(
                        id=uuid4(),
                        siniestro_id=asignado.id,
                        accion=ACCION_ASIGNADO,
                        ocurrido_en=ahora,
                        estado_anterior=EstadoSiniestro.REPORTADO,
                        estado_nuevo=EstadoSiniestro.EN_EVALUACION,
                        actor_id=actor.id,
                        actor_rol=actor.rol,
                        actor_nombre=actor.nombre,
                    )
                )
                await tx.evento_en_evaluacion(asignado, ahora)
                await tx.guardar_idempotencia(
                    clave_guardada, huella, {"siniestroId": str(asignado.id)}, ahora
                )
            op.marcar(RESULTADO_ASIGNADO)
            return resumen_de(asignado, ahora, self._plazos)
