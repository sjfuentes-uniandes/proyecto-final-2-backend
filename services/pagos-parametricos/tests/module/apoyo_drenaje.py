"""App de pagos-parametricos para la prueba de SIGTERM (HU-W25, paso 25.5).

La sirve uvicorn en un subproceso, con los argumentos del CMD del Dockerfile
(test_hu_w25_drenaje.py). Es la app real: `crear_app`, su lifespan, `_consumidores`,
`armar_consumidores`, `ConsumidorInbox` y `ReceptorSqs`. Los dobles reemplazan solo lo que tiene red
o base de datos:

- el cliente SQS (`main.cliente_sqs`): dos colas en memoria. Cada una entrega primero su mensaje
  inicial; sus tardíos llegan a la espera larga que esté abierta cuando aparece el archivo
  `DRENAJE_LLEGADA` (la prueba lo crea después del SIGTERM). Sin la variable, no llegan;
- el inbox (`main.InboxPg`): en memoria, con commit y rollback;
- los manejadores (`main.manejador_eventos` y `main.manejador_polizas`): tardan `DRENAJE_DEMORA_S`;
- el pool: solo responde a `/health`; y `siniestros`, que no se llama.

Cada acción del doble se imprime en una línea `DOBLE <acción> <cola> <id>`, que la prueba lee.
"""

import asyncio
import json
import os
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import FastAPI
from pagos_parametricos import main
from pagos_parametricos.config import Settings
from solventa_common.inbox import ESPERA_LARGA_S, EventoRecibido

DEMORA = "DRENAJE_DEMORA_S"
LLEGADA = "DRENAJE_LLEGADA"
URL_EVENTOS = "memoria://parametricos-eventos"
URL_POLIZAS = "memoria://parametricos-polizas"


def anotar(accion: str, cola: str, identificador: object) -> None:
    print(f"DOBLE {accion} {cola} {identificador}", flush=True)


def _id(nombre: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"hu-w25/drenaje/{nombre}"))


@dataclass
class Mensaje:
    id: str
    tipo: str

    def a_sqs(self) -> dict[str, Any]:
        sobre = {"eventId": self.id, "type": self.tipo, "correlationId": f"corr-{self.id}"}
        return {
            "MessageId": self.id,
            "ReceiptHandle": self.id,
            "Body": json.dumps(sobre),
            "MessageAttributes": {"eventType": {"DataType": "String", "StringValue": self.tipo}},
            "Attributes": {"ApproximateReceiveCount": "1"},
        }


@dataclass
class Cola:
    nombre: str
    visibles: list[Mensaje]
    tardios: list[Mensaje]
    todos: dict[str, Mensaje] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.todos = {m.id: m for m in self.visibles + self.tardios}
        iniciales = ",".join(m.id for m in self.visibles)
        tardios = ",".join(m.id for m in self.tardios)
        anotar("cola", self.nombre, f"inicial={iniciales};tardios={tardios}")


class ClienteSqsEnMemoria:
    """Lo que `ReceptorSqs` usa de boto3. La espera larga bloquea su hilo, como la de verdad."""

    def __init__(self, colas: dict[str, Cola], llegada: Path | None) -> None:
        self.meta = SimpleNamespace(config=SimpleNamespace(read_timeout=ESPERA_LARGA_S + 5))
        self._colas = colas
        self._llegada = llegada
        self._candado = threading.Lock()

    def receive_message(
        self, *, QueueUrl: str, MaxNumberOfMessages: int, WaitTimeSeconds: int, **_: Any
    ) -> dict[str, Any]:
        cola = self._colas[QueueUrl]
        limite = time.monotonic() + WaitTimeSeconds
        while True:
            with self._candado:
                if cola.tardios and self._llegada is not None and self._llegada.exists():
                    cola.visibles.extend(cola.tardios)
                    cola.tardios.clear()
                if cola.visibles:
                    entregados = cola.visibles[:MaxNumberOfMessages]
                    del cola.visibles[:MaxNumberOfMessages]
                    for mensaje in entregados:
                        anotar("recibido", cola.nombre, mensaje.id)
                    return {"Messages": [m.a_sqs() for m in entregados]}
            if time.monotonic() >= limite:
                return {"Messages": []}
            time.sleep(0.02)

    def delete_message(self, *, QueueUrl: str, ReceiptHandle: str) -> None:
        anotar("borrado", self._colas[QueueUrl].nombre, ReceiptHandle)

    def change_message_visibility(
        self, *, QueueUrl: str, ReceiptHandle: str, VisibilityTimeout: int
    ) -> None:
        cola = self._colas[QueueUrl]
        if VisibilityTimeout != 0:
            anotar("extendido", cola.nombre, ReceiptHandle)
            return
        anotar("liberado", cola.nombre, ReceiptHandle)
        with self._candado:
            cola.visibles.append(cola.todos[ReceiptHandle])


CONFIRMADOS: set[UUID] = set()


class TransaccionMemoria:
    def __init__(self) -> None:
        self.nuevos: list[UUID] = []

    async def registrar(self, event_id: UUID, event_type: str, recibido_en: datetime) -> bool:
        if event_id in CONFIRMADOS or event_id in self.nuevos:
            return False
        self.nuevos.append(event_id)
        return True


class InboxMemoria:
    def __init__(self, pool: Any) -> None: ...

    @asynccontextmanager
    async def transaccion(self) -> AsyncIterator[TransaccionMemoria]:
        tx = TransaccionMemoria()
        try:
            yield tx
        except BaseException:  # también la cancelación al vencer el plazo de la parada
            for evento in tx.nuevos:
                anotar("rollback", "-", evento)
            raise
        CONFIRMADOS.update(tx.nuevos)
        for evento in tx.nuevos:
            anotar("commit", "-", evento)


def manejador(cola: str, demora_s: float) -> Any:
    async def manejar(evento: EventoRecibido, tx: Any) -> None:
        anotar("empieza", cola, evento.event_id)
        await asyncio.sleep(demora_s)
        anotar("termina", cola, evento.event_id)

    return manejar


class ConexionSinBase:
    async def execute(self, consulta: str) -> None: ...


class PoolSinBase:
    @asynccontextmanager
    async def connection(self) -> AsyncIterator[ConexionSinBase]:
        yield ConexionSinBase()


class SiniestrosSinRed:
    async def crear(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no debía llamarse a siniestros")


def crear() -> FastAPI:
    demora_s = float(os.environ.get(DEMORA, "0"))
    llegada = Path(os.environ[LLEGADA]) if os.environ.get(LLEGADA) else None
    colas = {
        URL_EVENTOS: Cola(
            "eventos",
            [Mensaje(_id("eventos-inicial"), "EventoParametricoObservado")],
            [Mensaje(_id(f"eventos-tardio-{i}"), "EventoParametricoObservado") for i in range(3)],
        ),
        URL_POLIZAS: Cola(
            "polizas",
            [Mensaje(_id("polizas-inicial"), "PolizaEmitida")],
            [Mensaje(_id("polizas-tardio"), "PolizaEmitida")],
        ),
    }
    cliente = ClienteSqsEnMemoria(colas, llegada)
    main.cliente_sqs = lambda region, endpoint_url=None: cliente  # type: ignore[assignment]
    main.InboxPg = InboxMemoria  # type: ignore[assignment,misc]
    main.manejador_eventos = lambda evaluar, activar: manejador("eventos", demora_s)  # type: ignore[assignment]
    main.manejador_polizas = lambda aplicar: manejador("polizas", demora_s)  # type: ignore[assignment]
    settings = Settings(
        SQS_PARAMETRICOS_EVENTOS_URL=URL_EVENTOS,
        SQS_PARAMETRICOS_POLIZAS_URL=URL_POLIZAS,
        SQS_CONCURRENCIA=10,
        OUTBOX_ENABLED=False,
    )
    return main.crear_app(settings, pool=PoolSinBase(), siniestros=SiniestrosSinRed())  # type: ignore[arg-type]


app = crear()
