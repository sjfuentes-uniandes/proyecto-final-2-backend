"""Fixtures compartidas: repositorio en memoria y telemetría (patrón de adaptador-identidad)."""

import io
import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID, uuid4

import pytest
from clientes.domain.identidad.modelos import Cliente, RegistroYaVinculado
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from solventa_common import telemetry
from solventa_common.logging import FormateadorJson
from solventa_common.settings import ServiceSettings


class RepositorioMemoria:
    """RepositorioClientes en memoria con los mismos UNIQUE que la tabla (`sub` y registro)."""

    def __init__(self) -> None:
        self.clientes: dict[UUID, Cliente] = {}

    def obtener(self, cliente_id: UUID) -> Cliente | None:
        return self.clientes.get(cliente_id)

    def por_sub(self, sub: str) -> Cliente | None:
        return next((c for c in self.clientes.values() if c.sub == sub), None)

    def por_registro(self, registro_id: UUID) -> Cliente | None:
        return next((c for c in self.clientes.values() if c.registro_id == registro_id), None)

    def asegurar(self, sub: str) -> Cliente:
        existente = self.por_sub(sub)
        if existente is not None:
            return existente
        nuevo = Cliente(id=uuid4(), sub=sub)
        self.clientes[nuevo.id] = nuevo
        return nuevo

    def asegurar_registro(self, registro_id: UUID, correo: str) -> Cliente:
        existente = self.por_registro(registro_id)
        if existente is not None:
            return existente
        nuevo = Cliente(id=uuid4(), registro_id=registro_id, correo=correo)
        self.clientes[nuevo.id] = nuevo
        return nuevo

    def vincular(self, cliente_id: UUID, sub: str) -> Cliente:
        cliente = self.clientes[cliente_id]
        dueno = self.por_sub(sub)
        if (dueno is not None and dueno.id != cliente_id) or cliente.sub not in (None, sub):
            raise RegistroYaVinculado()
        vinculado = replace(cliente, sub=sub)
        self.clientes[cliente_id] = vinculado
        return vinculado

    def guardar(self, cliente: Cliente) -> None:
        self.clientes[cliente.id] = cliente


@pytest.fixture
def repositorio() -> RepositorioMemoria:
    return RepositorioMemoria()


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

    def texto_spans(self) -> str:
        return " ".join(
            f"{s.name} {dict(s.attributes or {})} {[dict(e.attributes or {}) for e in s.events]}"
            for s in self.lista_spans()
        )

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

    def capturar(servicio: str = "clientes", ambiente: str = "pruebas") -> io.StringIO:
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
        ServiceSettings(service_name="clientes", environment="pruebas"),
        span_exporter=spans,
        metric_readers=[lector],
    )
    return Telemetria(spans=spans, lector=lector, salida=capturar_logs())
