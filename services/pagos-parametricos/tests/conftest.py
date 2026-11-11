"""Fixtures de telemetría y logs (patrón de adaptador-pagos/tests/conftest.py)."""

import io
import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pagos_parametricos.domain.parametrico.modelos import (
    Cobertura,
    EvaluacionEvento,
    EventoObservado,
)
from solventa_common import telemetry
from solventa_common.inbox import EventoRecibido, MensajeSqs, leer_sobre
from solventa_common.logging import FormateadorJson
from solventa_common.settings import ServiceSettings


@dataclass
class Telemetria:
    spans: InMemorySpanExporter
    lector: InMemoryMetricReader
    salida: io.StringIO

    def logs(self) -> list[dict[str, Any]]:
        return [json.loads(linea) for linea in self.salida.getvalue().splitlines() if linea.strip()]

    def texto_logs(self) -> str:
        return self.salida.getvalue()

    def lista_spans(self) -> list[ReadableSpan]:
        return list(self.spans.get_finished_spans())

    def puntos(self, nombre: str) -> list[Any]:
        datos = self.lector.get_metrics_data()
        if datos is None:
            return []
        return [
            punto
            for recurso in datos.resource_metrics
            for alcance in recurso.scope_metrics
            for metrica in alcance.metrics
            if metrica.name == nombre
            for punto in metrica.data.data_points
        ]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def capturar_logs() -> Iterator[Callable[..., io.StringIO]]:
    """Redirige el logger raíz a un buffer con el formateador JSON."""
    raiz = logging.getLogger()
    anteriores, nivel = list(raiz.handlers), raiz.level

    def capturar(servicio: str = "pagos-parametricos", ambiente: str = "pruebas") -> io.StringIO:
        salida = io.StringIO()
        handler = logging.StreamHandler(salida)
        handler.setFormatter(FormateadorJson(servicio, ambiente))
        raiz.handlers = [handler]
        raiz.setLevel(logging.INFO)
        return salida

    yield capturar
    raiz.handlers, raiz.level = anteriores, nivel


@pytest.fixture
def telemetria(
    monkeypatch: pytest.MonkeyPatch, capturar_logs: Callable[..., io.StringIO]
) -> Telemetria:
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    spans = InMemorySpanExporter()
    lector = InMemoryMetricReader()
    telemetry.configurar(
        ServiceSettings(service_name="pagos-parametricos", environment="pruebas"),
        span_exporter=spans,
        metric_readers=[lector],
    )
    return Telemetria(spans=spans, lector=lector, salida=capturar_logs())


# --- Datos sintéticos de R5 (contracts/datos-sinteticos/eventos-parametricos.json) ----------------
DATOS = Path(__file__).parents[3] / "contracts" / "datos-sinteticos" / "eventos-parametricos.json"
RECIBIDO_EN = "2026-11-10T14:00:05Z"


@pytest.fixture(scope="session")
def datos() -> dict[str, Any]:
    cargados: dict[str, Any] = json.loads(DATOS.read_text(encoding="utf-8"))
    return cargados


@pytest.fixture(scope="session")
def casos(datos: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {caso["clave"]: caso for caso in datos["eventos"]}


@pytest.fixture(scope="session")
def coberturas_sinteticas(datos: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["clave"]: c for c in datos["coberturas"]}


def sobre_observado_de(caso: dict[str, Any]) -> dict[str, Any]:
    """El `EventoParametricoObservado` que publicaría `adaptador-telemetria` para un caso del archivo
    de datos (la misma identidad: uuid5 de la fuente y el `idExterno`)."""
    envio = caso["envio"]
    cuerpo = envio["cuerpo"]
    evento_id = str(uuid5(NAMESPACE_URL, f"telemetria/{envio['fuente']}/{cuerpo['idExterno']}"))
    return {
        "eventId": evento_id,
        "aggregateId": evento_id,
        "type": "EventoParametricoObservado",
        "version": 1,
        "occurredAt": RECIBIDO_EN,
        "correlationId": f"corr-{caso['clave']}",
        "payload": {
            "eventoId": evento_id,
            "fuente": envio["fuente"],
            "idExterno": cuerpo["idExterno"],
            "eventoOrigen": cuerpo["eventoOrigen"],
            "tipoEvento": cuerpo["tipoEvento"],
            "valorObservado": cuerpo["valorObservado"],
            "unidad": cuerpo["unidad"],
            "zona": cuerpo["zona"],
            "observadoEn": cuerpo["observadoEn"],
            "recibidoEn": RECIBIDO_EN,
            "archivoRef": f"telemetria/2026/11/10/{evento_id}.json",
        },
    }


def a_recibido(sobre: dict[str, Any] | str, message_id: str = "msg-1") -> EventoRecibido:
    cuerpo = sobre if isinstance(sobre, str) else json.dumps(sobre)
    return leer_sobre(MensajeSqs(message_id, "recibo", cuerpo, None, 1))


@pytest.fixture
def observado() -> Callable[[dict[str, Any]], dict[str, Any]]:
    return sobre_observado_de


@pytest.fixture
def recibido() -> Callable[..., EventoRecibido]:
    return a_recibido


# --- Repositorios en memoria ---------------------------------------------------------------------
class Tx:
    """La transacción del inbox de las pruebas: los repositorios en memoria no la usan."""


class CoberturasMemoria:
    def __init__(self) -> None:
        self.filas: dict[UUID, Cobertura] = {}
        self.bloqueadas: list[UUID] = []

    async def bloquear_y_leer(self, tx: Any, poliza_id: UUID) -> Cobertura | None:
        self.bloqueadas.append(poliza_id)
        return self.filas.get(poliza_id)

    async def leer(self, tx: Any, poliza_id: UUID) -> Cobertura | None:
        return self.filas.get(poliza_id)

    async def guardar(self, tx: Any, cobertura: Cobertura) -> None:
        self.filas[cobertura.poliza_id] = cobertura

    async def candidatas(self, tx: Any, tipo_evento: str, zona: str) -> list[Cobertura]:
        return [
            c
            for c in self.filas.values()
            if c.estado.value == "ACTIVA"
            and c.regla is not None
            and (c.regla.tipo_evento, c.regla.zona) == (tipo_evento, zona)
        ]


@dataclass
class Registro:
    evento: EventoObservado
    resultado: EvaluacionEvento
    evaluado_en: datetime
    correlation_id: str


@dataclass
class EvaluacionesMemoria:
    registros: dict[UUID, Registro] = field(default_factory=dict)
    duplicados: dict[UUID, int] = field(default_factory=dict)

    async def descartar_duplicado(self, tx: Any, evento_id: UUID) -> bool:
        if evento_id not in self.registros:
            return False
        self.duplicados[evento_id] = self.duplicados.get(evento_id, 0) + 1
        return True

    async def registrar(
        self,
        tx: Any,
        evento: EventoObservado,
        resultado: EvaluacionEvento,
        evaluado_en: datetime,
        correlation_id: str,
    ) -> None:
        assert evento.evento_id not in self.registros
        self.registros[evento.evento_id] = Registro(evento, resultado, evaluado_en, correlation_id)


class RelojFijo:
    def __init__(self, ahora: datetime | None = None) -> None:
        self._ahora = ahora or datetime(2026, 11, 10, 14, 0, 6, tzinfo=UTC)

    def ahora(self) -> datetime:
        return self._ahora


@pytest.fixture
def coberturas() -> CoberturasMemoria:
    return CoberturasMemoria()


@pytest.fixture
def evaluaciones() -> EvaluacionesMemoria:
    return EvaluacionesMemoria()


@pytest.fixture
def reloj() -> RelojFijo:
    return RelojFijo()


@pytest.fixture
def tx() -> Tx:
    return Tx()
