"""Puertos de la evaluación paramétrica (HU-W21). `T` es la transacción del inbox: el efecto de
cada mensaje se escribe en ella (modo (a) de `solventa_common.inbox`)."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from pagos_parametricos.domain.parametrico.modelos import (
    Cobertura,
    EvaluacionEvento,
    EventoObservado,
)


class RepositorioCoberturas[T](Protocol):
    async def bloquear_y_leer(self, tx: T, poliza_id: UUID) -> Cobertura | None:
        """La cobertura de la póliza, con la póliza bloqueada hasta el fin de la transacción: dos
        eventos de la misma póliza a la vez se aplican en serie."""
        ...

    async def leer(self, tx: T, poliza_id: UUID) -> Cobertura | None:
        """La cobertura de la póliza, sin bloquearla."""
        ...

    async def guardar(self, tx: T, cobertura: Cobertura) -> None: ...

    async def candidatas(self, tx: T, tipo_evento: str, zona: str) -> list[Cobertura]:
        """Solo las `ACTIVA` del mismo tipo y zona, por índice (DS3-19)."""
        ...


class RepositorioEvaluaciones[T](Protocol):
    async def descartar_duplicado(self, tx: T, evento_id: UUID) -> bool:
        """True si el evento ya tenía su decisión: suma un duplicado y no cambia nada más."""
        ...

    async def registrar(
        self,
        tx: T,
        evento: EventoObservado,
        resultado: EvaluacionEvento,
        evaluado_en: datetime,
        correlation_id: str,
    ) -> None:
        """En la transacción: `eventos_evaluados`, `evaluaciones`, `activaciones` `PENDIENTE` y
        `EventoParametricoEvaluado` en el Outbox."""
        ...


class Reloj(Protocol):
    def ahora(self) -> datetime: ...
