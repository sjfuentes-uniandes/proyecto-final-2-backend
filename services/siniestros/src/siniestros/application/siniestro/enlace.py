"""URL prefirmada de una evidencia (HU-W19, DS3-08).

Se emite solo aquí, al abrir el archivo: vive `ttl_s` segundos (≤ 300), no se guarda, no se registra
y no aparece en el detalle. Antes de firmar se confirma que el objeto existe; si no, 404 y sin URL.
Cada emisión deja `EvidenciaConsultada` en el Outbox (sin la URL ni la clave del objeto).
"""

from datetime import timedelta
from uuid import UUID

from solventa_common.telemetry import RESULTADO_RECHAZADO, operacion

from siniestros.application.siniestro.errores import (
    DependenciaNoDisponible,
    EvidenciaNoDisponible,
    EvidenciaNoEncontrada,
    SiniestroNoEncontrado,
)
from siniestros.application.siniestro.puertos import (
    AlmacenEvidencias,
    Reloj,
    SiniestrosRepositorio,
)
from siniestros.application.siniestro.vistas import Actor, EnlaceEmitido
from siniestros.domain.siniestro.modelos import IntegridadEvidencia

TIPO_CONTENIDO_GENERICO = "application/octet-stream"
RESULTADO_NO_DISPONIBLE = "no_disponible"


class EmitirEnlace:
    def __init__(
        self, repo: SiniestrosRepositorio, almacen: AlmacenEvidencias, reloj: Reloj, ttl_s: int
    ) -> None:
        self._repo = repo
        self._almacen = almacen
        self._reloj = reloj
        self._ttl_s = ttl_s

    async def ejecutar(self, siniestro_id: UUID, evidencia_id: UUID, actor: Actor) -> EnlaceEmitido:
        with operacion("evidencia.enlace") as op:
            existe, evidencia = await self._repo.evidencia(siniestro_id, evidencia_id)
            if not existe:
                op.marcar(RESULTADO_RECHAZADO)
                raise SiniestroNoEncontrado
            if evidencia is None:
                op.marcar(RESULTADO_RECHAZADO)
                raise EvidenciaNoEncontrada

            integridad = await self._almacen.verificar(evidencia.clave_objeto, evidencia.sha256)
            if integridad is IntegridadEvidencia.NO_DISPONIBLE:
                op.marcar(RESULTADO_NO_DISPONIBLE)
                raise EvidenciaNoDisponible
            if integridad is IntegridadEvidencia.NO_VERIFICABLE:
                # No se puede confirmar que el objeto exista: no se firma a ciegas.
                raise DependenciaNoDisponible("evidencias")

            tipo_contenido = evidencia.tipo_contenido or TIPO_CONTENIDO_GENERICO
            url = await self._almacen.firmar_lectura(
                evidencia.clave_objeto, self._ttl_s, tipo_contenido
            )
            ahora = self._reloj.ahora()
            expira_en = ahora + timedelta(seconds=self._ttl_s)
            async with self._repo.transaccion() as tx:
                await tx.evento_evidencia_consultada(evidencia, actor.id, expira_en, ahora)
            return EnlaceEmitido(url=url, expira_en=expira_en, tipo_contenido=tipo_contenido)
