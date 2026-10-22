"""Lecturas de la suscripción (HU-W13): una decisión, la de una cotización, la bandeja de casos
en revisión (E01) y sus indicadores (DS2-14).

La bandeja se pagina por cursor (decidida_en, id): estable aunque lleguen casos nuevos. El cursor
es opaco para el portal; uno que no entregó polizas es una solicitud inválida.
"""

import base64
import binascii
import dataclasses
from datetime import datetime, timedelta
from uuid import UUID

from solventa_common.telemetry import RESULTADO_RECHAZADO, operacion

from polizas.application.suscripcion.puertos import DecisionesRepositorio, Reloj
from polizas.domain.suscripcion.errores import CursorInvalido, DecisionNoEncontrada, RangoInvalido
from polizas.domain.suscripcion.modelos import (
    CasoBandeja,
    Decision,
    FiltrosBandeja,
    PaginaBandeja,
    Resumen,
)


def codificar_cursor(caso: CasoBandeja) -> str:
    crudo = f"{caso.decidida_en.isoformat()}|{caso.decision_id}"
    return base64.urlsafe_b64encode(crudo.encode()).decode()


def decodificar_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        instante, identificador = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        decidida_en = datetime.fromisoformat(instante)
        decision_id = UUID(identificador)
    except (binascii.Error, UnicodeDecodeError, ValueError) as error:
        raise CursorInvalido() from error
    if decidida_en.tzinfo is None:
        raise CursorInvalido()
    return decidida_en, decision_id


def antiguedad_minutos(decidida_en: datetime, ahora: datetime) -> int:
    """Minutos completos en cola; nunca negativo (relojes entre réplicas)."""
    return max(int((ahora - decidida_en).total_seconds() // 60), 0)


class ConsultarSuscripciones:
    def __init__(self, repo: DecisionesRepositorio, reloj: Reloj, compromiso_horas: int) -> None:
        self._repo = repo
        self._reloj = reloj
        self.compromiso_horas = compromiso_horas

    async def decision(self, decision_id: UUID) -> Decision:
        with operacion("suscripcion.consultar") as op:
            decision = await self._repo.por_id(decision_id)
            if decision is None:
                op.marcar(RESULTADO_RECHAZADO)
                raise DecisionNoEncontrada()
        return decision

    async def por_cotizacion(self, cotizacion_id: UUID) -> Decision | None:
        with operacion("suscripcion.consultar"):
            return await self._repo.por_cotizacion(cotizacion_id)

    async def bandeja(
        self, filtros: FiltrosBandeja, limite: int, cursor: str | None
    ) -> PaginaBandeja:
        with operacion("suscripcion.bandeja") as op:
            try:
                despues = decodificar_cursor(cursor) if cursor else None
            except CursorInvalido:
                op.marcar(RESULTADO_RECHAZADO)
                raise
            casos = await self._repo.bandeja(filtros, limite + 1, despues)
            total = await self._repo.contar_bandeja(filtros)
        ahora = self._reloj.ahora()
        pagina = [
            dataclasses.replace(c, antiguedad_minutos=antiguedad_minutos(c.decidida_en, ahora))
            for c in casos[:limite]
        ]
        siguiente = codificar_cursor(pagina[-1]) if len(casos) > limite else None
        return PaginaBandeja(tuple(pagina), siguiente, total)

    async def resumen(self, desde: datetime, hasta: datetime) -> Resumen:
        with operacion("suscripcion.resumen") as op:
            if desde >= hasta:
                op.marcar(RESULTADO_RECHAZADO)
                raise RangoInvalido()
            resueltas, en_revision, media_ms = await self._repo.contar_dia(desde, hasta)
            limite = self._reloj.ahora() - timedelta(hours=self.compromiso_horas)
            fuera = await self._repo.contar_fuera_del_compromiso(limite)
        return Resumen(
            desde=desde,
            hasta=hasta,
            resueltas_automaticamente=resueltas,
            enviadas_a_revision=en_revision,
            total=resueltas + en_revision,
            tiempo_medio_decision_ms=None if media_ms is None else round(media_ms),
            fuera_del_compromiso=fuera,
            compromiso_revision_horas=self.compromiso_horas,
        )
