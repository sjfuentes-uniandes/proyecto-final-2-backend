"""Puertos de la evaluación paramétrica (HU-W21). `T` es la transacción del inbox: el efecto de
cada mensaje se escribe en ella (modo (a) de `solventa_common.inbox`)."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from pagos_parametricos.domain.parametrico.activacion import (
    ActivacionPendiente,
    Disparador,
    SiniestroCreado,
    SolicitudSiniestro,
)
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


class RepositorioActivaciones(Protocol):
    """Las activaciones de W21 que W22 convierte en siniestros (HU-W22). A diferencia de los
    repositorios de arriba no recibe la transacción del inbox: la continuación corre **después** del
    commit y cada operación es una transacción corta propia (nunca se retiene una conexión mientras
    se espera a `siniestros`)."""

    async def pendientes(self, evento_id: UUID) -> list[ActivacionPendiente]:
        """Las activaciones `PENDIENTE` del evento con la cobertura de su póliza."""
        ...

    async def marcar_creada(
        self,
        evento_id: UUID,
        poliza_id: UUID,
        siniestro: SiniestroCreado,
        disparador: Disparador,
    ) -> None:
        """`PENDIENTE` → `SINIESTRO_CREADO` con el siniestro, su código y el disparador, en una
        transacción corta. Idempotente: una activación ya creada no cambia."""
        ...

    async def sumar_duplicado(self, evento_id: UUID) -> None:
        """Una entrega repetida del evento que se reanuda (DS3-15): suma 1 a `duplicados`."""
        ...


class Siniestros(Protocol):
    """`POST /v1/siniestros/parametricos` de `siniestros` (DS3-23), detrás de su ACL."""

    async def crear(self, solicitud: SolicitudSiniestro) -> SiniestroCreado:
        """Lanza `TiempoAgotado`, `DependenciaNoDisponible` o `SiniestrosRechazo`."""
        ...
