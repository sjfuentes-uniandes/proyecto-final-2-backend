"""Fixtures de telemetría y logs (patrón de pagos-parametricos y adaptador-pagos)."""

import io
import json
import logging
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from adaptador_telemetria.application.telemetria.puertos import (
    DependenciaNoDisponible,
    Registrado,
)
from adaptador_telemetria.application.telemetria.recibir_evento import RecibirEvento
from adaptador_telemetria.domain.telemetria.firma import calcular_firma
from adaptador_telemetria.domain.telemetria.modelos import EventoCanonico
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from solventa_common import telemetry
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

    def capturar(servicio: str = "adaptador-telemetria", ambiente: str = "pruebas") -> io.StringIO:
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
        ServiceSettings(service_name="adaptador-telemetria", environment="pruebas"),
        span_exporter=spans,
        metric_readers=[lector],
    )
    return Telemetria(spans=spans, lector=lector, salida=capturar_logs())


# --- Datos sintéticos de R5 (contracts/datos-sinteticos/eventos-parametricos.json) ----------------
DATOS = Path(__file__).parents[3] / "contracts" / "datos-sinteticos" / "eventos-parametricos.json"
# Solo pruebas: el de compose.yaml es otro y el de INT vive en Secrets Manager.
SECRETO = "secreto-de-prueba"
AHORA = datetime(2026, 11, 10, 14, 0, 5, tzinfo=UTC)


@dataclass(frozen=True)
class Envio:
    """Lo que la fuente manda: los tres encabezados y los bytes del cuerpo."""

    fuente: str
    marca_tiempo: str
    firma: str | None
    cuerpo: bytes

    def encabezados(self) -> dict[str, str]:
        encabezados = {
            "X-Telemetria-Fuente": self.fuente,
            "X-Telemetria-Marca-Tiempo": self.marca_tiempo,
        }
        if self.firma is not None:
            encabezados["X-Telemetria-Firma"] = self.firma
        return encabezados


def armar_envio(caso: dict[str, Any], ahora: datetime, secreto: str = SECRETO) -> Envio:
    """Firma el envío de un caso del archivo de datos: `desfaseSegundos` es relativo a `ahora` y
    `firma` es VALIDA, ALTERADA (último dígito cambiado) o AUSENTE."""
    datos = caso["envio"]
    marca = (ahora + timedelta(seconds=datos["marcaTiempo"]["desfaseSegundos"])).isoformat()
    marca = marca.replace("+00:00", "Z")
    if "cuerpoCrudo" in datos:
        cuerpo = datos["cuerpoCrudo"].encode()
    else:
        cuerpo = json.dumps(datos["cuerpo"], separators=(",", ":")).encode()
    firma: str | None = calcular_firma(secreto, marca, cuerpo)
    if datos["firma"] == "ALTERADA":
        assert firma is not None
        firma = firma[:-1] + ("0" if firma[-1] != "0" else "1")
    elif datos["firma"] == "AUSENTE":
        firma = None
    return Envio(datos["fuente"], marca, firma, cuerpo)


@pytest.fixture(scope="session")
def datos() -> dict[str, Any]:
    cargados: dict[str, Any] = json.loads(DATOS.read_text(encoding="utf-8"))
    return cargados


@pytest.fixture(scope="session")
def casos(datos: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {caso["clave"]: caso for caso in datos["eventos"]}


@pytest.fixture
def firmar_caso() -> Callable[..., Envio]:
    return armar_envio


# --- Puertos en memoria ----------------------------------------------------------------------------
class ArchivoFalso:
    def __init__(self, falla: bool = False) -> None:
        self.guardados: list[tuple[str, bytes, Mapping[str, str]]] = []
        self.falla = falla

    async def guardar(self, clave: str, cuerpo: bytes, metadatos: Mapping[str, str]) -> None:
        if self.falla:
            raise DependenciaNoDisponible("archivo")
        self.guardados.append((clave, cuerpo, metadatos))


class RegistroFalso:
    def __init__(self) -> None:
        self.eventos: dict[UUID, datetime] = {}
        self.llamadas: list[str] = []
        self.falla_lectura = False
        self.falla_registro = False
        self.gana_otro: datetime | None = None
        self.outbox: list[EventoCanonico] = []

    async def recibido(self, evento_id: UUID) -> datetime | None:
        self.llamadas.append("recibido")
        if self.falla_lectura:
            raise DependenciaNoDisponible("base")
        return self.eventos.get(evento_id)

    async def registrar(
        self, evento: EventoCanonico, recibido_en: datetime, archivo_ref: str, correlation_id: str
    ) -> Registrado:
        self.llamadas.append("registrar")
        if self.falla_registro:
            raise DependenciaNoDisponible("base")
        if self.gana_otro is not None:
            return Registrado(nuevo=False, recibido_en=self.gana_otro)
        self.eventos[evento.evento_id] = recibido_en
        self.outbox.append(evento)
        return Registrado(nuevo=True, recibido_en=recibido_en)


class RelojFijo:
    def __init__(self, ahora: datetime = AHORA) -> None:
        self._ahora = ahora

    def ahora(self) -> datetime:
        return self._ahora


@pytest.fixture
def archivo() -> ArchivoFalso:
    return ArchivoFalso()


@pytest.fixture
def registro() -> RegistroFalso:
    return RegistroFalso()


@pytest.fixture
def crear_caso(archivo: ArchivoFalso, registro: RegistroFalso) -> Callable[..., RecibirEvento]:
    """`RecibirEvento` sobre los puertos en memoria; `reloj` y `secreto` se pueden cambiar."""

    def crear(reloj: datetime | None = None, secreto: str | None = SECRETO) -> RecibirEvento:
        return RecibirEvento(
            secreto=secreto,
            fuentes=("IDEAM-SIM", "AEROCIVIL-SIM"),
            desfase_max_s=300,
            prefijo_archivo="telemetria/",
            archivo=archivo,
            registro=registro,
            reloj=RelojFijo(reloj or AHORA),
        )

    return crear


@pytest.fixture
def caso(crear_caso: Callable[..., RecibirEvento]) -> RecibirEvento:
    return crear_caso()


@pytest.fixture
def reloj_fijo() -> RelojFijo:
    return RelojFijo()
