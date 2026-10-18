"""Fixtures compartidas: repositorio en memoria y telemetría (patrón de adaptador-identidad)."""

import io
import json
import logging
from calendar import monthrange
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from clientes.application.consentimientos.puertos import Otorgamiento
from clientes.domain.consentimientos.modelos import (
    ClaveIdempotenciaEnUso,
    Consentimiento,
    ConsentimientoActivoExistente,
    EstadoConsentimiento,
    RegistroIdempotencia,
)
from clientes.domain.identidad.modelos import Cliente, DatosCliente, RegistroYaVinculado
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

    def asegurar_por_documento(self, datos: DatosCliente) -> tuple[Cliente, bool]:
        existente = next(
            (
                c
                for c in self.clientes.values()
                if (c.tipo_documento, c.numero_documento)
                == (datos.tipo_documento, datos.numero_documento)
            ),
            None,
        )
        if existente is not None:
            return existente, False
        nuevo = Cliente(
            id=uuid4(),
            tipo_documento=datos.tipo_documento,
            numero_documento=datos.numero_documento,
            nombre_completo=datos.nombre_completo,
            fecha_nacimiento=datos.fecha_nacimiento,
        )
        self.clientes[nuevo.id] = nuevo
        return nuevo, True


@pytest.fixture
def repositorio() -> RepositorioMemoria:
    return RepositorioMemoria()


def sumar_meses(instante: datetime, meses: int) -> datetime:
    """Implementación de referencia de `fin_de_vigencia` (ADR 0010): si el día no existe en el
    mes de destino se usa el último día del mes; la hora se conserva."""
    total = instante.month - 1 + meses
    anio, mes = instante.year + total // 12, total % 12 + 1
    return instante.replace(year=anio, month=mes, day=min(instante.day, monthrange(anio, mes)[1]))


class RelojFalso:
    """Reloj de la base de datos: se controla desde la prueba."""

    def __init__(self) -> None:
        self.ahora = datetime(2026, 10, 17, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.ahora

    def avanzar(self, delta: timedelta) -> None:
        self.ahora += delta


@pytest.fixture
def reloj() -> RelojFalso:
    return RelojFalso()


class RepositorioConsentimientosMemoria:
    """RepositorioConsentimientos en memoria con las mismas reglas que la base: un solo
    `ACTIVO` por (cliente, propósito), clave de idempotencia por cliente con retención de 24 h
    y un evento por consentimiento creado."""

    def __init__(self, reloj: Callable[[], datetime]) -> None:
        self._reloj = reloj
        self.consentimientos: dict[UUID, Consentimiento] = {}
        self.claves: dict[tuple[UUID, UUID], tuple[str, UUID, datetime]] = {}
        self.eventos: list[dict[str, Any]] = []
        self.falla: Exception | None = None

    def _leer(self, c: Consentimiento) -> Consentimiento:
        return replace(c, leido_en=self._reloj())

    async def por_clave(self, cliente_id: UUID, clave: UUID) -> RegistroIdempotencia | None:
        registro = self.claves.get((cliente_id, clave))
        if registro is None or self._reloj() - registro[2] >= timedelta(hours=24):
            return None
        return RegistroIdempotencia(registro[0], self._leer(self.consentimientos[registro[1]]))

    async def activo(self, cliente_id: UUID, proposito: str) -> Consentimiento | None:
        for c in self.consentimientos.values():
            if (c.cliente_id, c.proposito, c.estado) == (
                cliente_id,
                proposito,
                EstadoConsentimiento.ACTIVO,
            ):
                return self._leer(c)
        return None

    async def crear(self, otorgamiento: Otorgamiento) -> Consentimiento:
        if self.falla is not None:
            raise self.falla
        ahora = self._reloj()
        self.claves = {k: v for k, v in self.claves.items() if ahora - v[2] < timedelta(hours=24)}
        solicitud = otorgamiento.solicitud
        for c in list(self.consentimientos.values()):
            if (c.cliente_id, c.proposito, c.estado) != (
                otorgamiento.cliente_id,
                solicitud.proposito,
                EstadoConsentimiento.ACTIVO,
            ):
                continue
            if c.vigente_hasta > ahora:
                raise ConsentimientoActivoExistente()
            self.consentimientos[c.id] = replace(c, estado=EstadoConsentimiento.VENCIDO)
        if (otorgamiento.cliente_id, otorgamiento.clave) in self.claves:
            raise ClaveIdempotenciaEnUso()
        creado = Consentimiento(
            id=otorgamiento.id,
            cliente_id=otorgamiento.cliente_id,
            proposito=solicitud.proposito,
            alcances=solicitud.alcances,
            version_texto=solicitud.version_texto,
            idioma=solicitud.idioma,
            estado=EstadoConsentimiento.ACTIVO,
            otorgado_en=ahora,
            vigente_hasta=sumar_meses(ahora, solicitud.vigencia_meses),
            revocado_en=None,
            leido_en=ahora,
        )
        self.consentimientos[creado.id] = creado
        self.claves[(otorgamiento.cliente_id, otorgamiento.clave)] = (
            otorgamiento.huella,
            creado.id,
            ahora,
        )
        self.eventos.append(
            {
                "tipo": "ConsentimientoOtorgado",
                "agregado": creado.id,
                "actor": otorgamiento.actor_id,
            }
        )
        return creado

    async def obtener(self, cliente_id: UUID, consentimiento_id: UUID) -> Consentimiento | None:
        c = self.consentimientos.get(consentimiento_id)
        return self._leer(c) if c is not None and c.cliente_id == cliente_id else None

    async def listar(self, cliente_id: UUID) -> list[Consentimiento]:
        propios = [c for c in self.consentimientos.values() if c.cliente_id == cliente_id]
        return [self._leer(c) for c in sorted(propios, key=lambda c: c.otorgado_en, reverse=True)]


@pytest.fixture
def consentimientos(reloj: RelojFalso) -> RepositorioConsentimientosMemoria:
    return RepositorioConsentimientosMemoria(reloj)


class PoolFalso:
    """Sustituye a AsyncConnectionPool: registra cómo se construye, abre y cierra."""

    def __init__(self, conninfo: str, **opciones: Any) -> None:
        self.conninfo = conninfo
        self.opciones = opciones
        self.eventos: list[str] = []

    async def open(self) -> None:
        self.eventos.append("open")

    async def close(self) -> None:
        self.eventos.append("close")


@pytest.fixture
def pools(monkeypatch: pytest.MonkeyPatch) -> list[PoolFalso]:
    """Los pools que crea `crear_app` sin repositorio inyectado, sin conectar a una base."""
    creados: list[PoolFalso] = []

    def crear(conninfo: str, **opciones: Any) -> PoolFalso:
        pool = PoolFalso(conninfo, **opciones)
        creados.append(pool)
        return pool

    monkeypatch.setattr("psycopg_pool.AsyncConnectionPool", crear)
    return creados


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
