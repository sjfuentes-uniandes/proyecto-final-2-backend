"""Fixtures compartidas de cotizacion: telemetría (patrón de libs/solventa-common), modelo
candidato 2026.10.1, datos de C01 (01 §3.5) y fakes de los puertos del perfilamiento."""

import asyncio
import io
import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from cotizacion.adapters.outbound.catalogo_http import ModeloRiesgoDto, a_modelo_riesgo
from cotizacion.application.perfilamiento.crear_perfil import (
    ComandoPerfil,
    ConfigPerfilamiento,
    CrearPerfil,
)
from cotizacion.domain.perfilamiento.errores import ConflictoIdempotencia
from cotizacion.domain.perfilamiento.modelo_riesgo import ModeloRiesgo
from cotizacion.domain.perfilamiento.modelos import (
    ContextoConsulta,
    DatosDeclarados,
    DecisionConsentimiento,
    EstadoCampo,
    EstadoFuente,
    EstadoIdentidad,
    Fuente,
    Perfil,
    ResultadoFuente,
    SolicitudFuente,
    ValorCampo,
    VerificacionConsentimiento,
)
from cotizacion.domain.perfilamiento.motor import FuenteEvaluada, evaluar
from cotizacion.domain.perfilamiento.plan_fuentes import plan_fuentes
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

    def capturar(servicio: str = "cotizacion", ambiente: str = "pruebas") -> io.StringIO:
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
        ServiceSettings(service_name="cotizacion", environment="pruebas"),
        span_exporter=spans,
        metric_readers=[lector],
    )
    return Telemetria(spans=spans, lector=lector, salida=capturar_logs())


# La semilla que publica catalogo: las pruebas evalúan exactamente el modelo desplegado.
SEMILLA = (
    Path(__file__).resolve().parents[2]
    / "catalogo/src/catalogo/adapters/outbound/semillas/modelo_2026_10_1.json"
)

# C01: declarados por defecto (edad a 2026-10-15) y respuestas nominales del simulador.
VARIABLES_C01 = {
    "edad": Decimal(36),
    "plazo_credito_meses": Decimal(180),
    "suma_asegurada": Decimal(180000000),
    "antiguedad_productos_anios": Decimal(7),
    "cuota_mensual_obligaciones": Decimal("1800000.00"),
    "entidades_con_deuda": Decimal(2),
    "moras_12m": Decimal(0),
    "ingreso_mensual_estimado": Decimal("6500000.00"),
    "coincidencias_listas_restrictivas": Decimal(0),
}
CONFIANZA_C01 = {
    Fuente.FA_PRODUCTOS_VIGENTES: Decimal("0.95"),
    Fuente.FA_HISTORIAL_PAGOS_12M: Decimal("0.91"),
    Fuente.FA_INGRESOS_AGREGADOS: Decimal("0.93"),
    Fuente.DA_REGISTROS_PUBLICOS: Decimal("0.88"),
}


@pytest.fixture
def definicion_candidata() -> dict[str, Any]:
    definicion: dict[str, Any] = json.loads(SEMILLA.read_text(encoding="utf-8"))
    return definicion


def respuesta_catalogo(definicion: dict[str, Any], version: str = "2026.10.1") -> dict[str, Any]:
    return {
        "version": version,
        "productoId": "vida-hipotecario",
        "vigenteDesde": "2026-10-01T00:00:00Z",
        "hash": f"sha256:{version}",
        "definicion": definicion,
    }


@pytest.fixture
def respuesta_candidata(definicion_candidata: dict[str, Any]) -> dict[str, Any]:
    return respuesta_catalogo(definicion_candidata)


@pytest.fixture
def modelo(respuesta_candidata: dict[str, Any]) -> ModeloRiesgo:
    return a_modelo_riesgo(ModeloRiesgoDto.model_validate(respuesta_candidata))


@pytest.fixture
def variables_c01() -> dict[str, Decimal]:
    return dict(VARIABLES_C01)


@pytest.fixture
def fuentes_c01() -> dict[Fuente, FuenteEvaluada]:
    return {f: FuenteEvaluada(EstadoFuente.CONSULTADA, c) for f, c in CONFIANZA_C01.items()}


# --- Perfil de C01 calculado con el modelo candidato --------------------------------------

CAMPOS_FUENTE = {
    Fuente.FA_PRODUCTOS_VIGENTES: (
        "antiguedad_productos_anios",
        "cuota_mensual_obligaciones",
        "entidades_con_deuda",
    ),
    Fuente.FA_HISTORIAL_PAGOS_12M: ("moras_12m",),
    Fuente.FA_INGRESOS_AGREGADOS: ("ingreso_mensual_estimado",),
    Fuente.DA_REGISTROS_PUBLICOS: ("coincidencias_listas_restrictivas",),
}
CALCULADO_EN = datetime(2026, 10, 15, 15, 0, 0, 212000, tzinfo=UTC)


@pytest.fixture
def perfil_c01(modelo: ModeloRiesgo, fuentes_c01: dict[Fuente, FuenteEvaluada]) -> Perfil:
    fuentes = tuple(
        ResultadoFuente(
            fuente=fuente,
            estado=EstadoFuente.CONSULTADA,
            campos_solicitados=campos,
            campos={c: ValorCampo(EstadoCampo.DISPONIBLE, VARIABLES_C01[c]) for c in campos},
            version_fuente="1.8" if fuente is Fuente.DA_REGISTROS_PUBLICOS else "3.1",
            confianza=CONFIANZA_C01[fuente],
            consultada_en=datetime(2026, 10, 15, 15, 0, 0, 100000, tzinfo=UTC),
            duracion_ms=52,
            proveedor="datos-abiertos"
            if fuente is Fuente.DA_REGISTROS_PUBLICOS
            else "open-finance",
        )
        for fuente, campos in CAMPOS_FUENTE.items()
    )
    return Perfil(
        id=UUID("00000000-0000-4000-a000-000000000001"),
        cliente_id=UUID("00000000-0000-4000-8000-000000000001"),
        producto_id="vida-hipotecario",
        idempotency_key=UUID("00000000-0000-4000-b000-000000000001"),
        huella_solicitud="sha256:huella-c01",
        datos_declarados=DatosDeclarados(date(1990, 4, 18), 180, Decimal(180000000)),
        insumos=dict(VARIABLES_C01),
        resultado=evaluar(modelo, VARIABLES_C01, fuentes_c01),
        fuentes=fuentes,
        consentimiento=VerificacionConsentimiento(
            decision=DecisionConsentimiento.PERMITIDO,
            consentimiento_id=UUID("00000000-0000-4000-9000-000000000001"),
            vigente_hasta=datetime(2027, 10, 1, tzinfo=UTC),
            alcances_permitidos=frozenset(Fuente),
            causa=None,
        ),
        version_modelo=modelo.version,
        hash_modelo=modelo.hash,
        perfil_anterior_id=None,
        calculado_en=CALCULADO_EN,
        duracion_ms=212,
        correlation_id="corr-c01",
        actor_id="asesor-sintetico",
    )


# --- Fakes de los puertos de crear_perfil -------------------------------------------------

AHORA = datetime(2026, 10, 15, 15, 0, tzinfo=UTC)
CLIENTE_C01 = UUID("00000000-0000-4000-8000-000000000001")
CONSENTIMIENTO_C01 = UUID("00000000-0000-4000-9000-000000000001")


class Llamadas:
    """Base de los fakes: registra cada llamada y puede dormir o lanzar."""

    def __init__(self, respuesta: Any, demora_s: float = 0) -> None:
        self.respuesta = respuesta
        self.demora_s = demora_s
        self.llamadas: list[tuple[Any, ...]] = []

    async def _responder(self, *argumentos: Any) -> Any:
        self.llamadas.append(argumentos)
        if self.demora_s:
            await asyncio.sleep(self.demora_s)
        if isinstance(self.respuesta, BaseException):
            raise self.respuesta
        return self.respuesta


class IdentidadFalsa(Llamadas):
    async def estado(self, cliente_id: UUID) -> Any:
        return await self._responder(cliente_id)


class ConsentimientoFalso(Llamadas):
    async def verificar(self, cliente_id: UUID, proposito: str, alcances: Any) -> Any:
        return await self._responder(cliente_id, proposito, list(alcances))


class ModelosFalsos(Llamadas):
    async def vigente(self, producto_id: str, en: datetime) -> Any:
        return await self._responder(producto_id, en)

    async def por_version(self, version: str) -> Any:
        return await self._responder(version)


def resultado_nominal(solicitud: SolicitudFuente, **cambios: Any) -> ResultadoFuente:
    """Lo que devuelve AdaptadorDatosHttp para C01, solo con los campos pedidos."""
    resultado = ResultadoFuente(
        fuente=solicitud.fuente,
        estado=EstadoFuente.CONSULTADA,
        campos_solicitados=solicitud.campos,
        campos={c: ValorCampo(EstadoCampo.DISPONIBLE, VARIABLES_C01[c]) for c in solicitud.campos},
        version_fuente="1.8" if solicitud.fuente is Fuente.DA_REGISTROS_PUBLICOS else "3.1",
        confianza=CONFIANZA_C01[solicitud.fuente],
        consultada_en=AHORA,
        duracion_ms=50,
        proveedor="simulador",
    )
    return replace(resultado, **cambios)


class EjecutorFalso:
    """Consulta nominal de C01; `cambios[fuente]` altera el resultado de esa fuente."""

    def __init__(self, cambios: dict[Fuente, dict[str, Any]] | None = None) -> None:
        self.cambios = cambios or {}
        self.llamadas: list[tuple[tuple[SolicitudFuente, ...], ContextoConsulta]] = []

    async def ejecutar(self, solicitudes: Any, contexto: ContextoConsulta) -> list[ResultadoFuente]:
        self.llamadas.append((tuple(solicitudes), contexto))
        return [resultado_nominal(s, **self.cambios.get(s.fuente, {})) for s in solicitudes]


class RepositorioPerfilesMemoria:
    def __init__(self) -> None:
        self.perfiles: dict[UUID, Perfil] = {}
        self.errores_al_guardar: list[Exception] = []
        self.lecturas_idempotencia = 0

    async def por_idempotencia(self, cliente_id: UUID, clave: UUID) -> Perfil | None:
        self.lecturas_idempotencia += 1
        return next(
            (
                p
                for p in self.perfiles.values()
                if p.cliente_id == cliente_id and p.idempotency_key == clave
            ),
            None,
        )

    async def guardar(self, perfil: Perfil) -> None:
        if self.errores_al_guardar:
            raise self.errores_al_guardar.pop(0)
        if await self.por_idempotencia(perfil.cliente_id, perfil.idempotency_key):
            raise ConflictoIdempotencia()
        self.perfiles[perfil.id] = perfil

    async def por_id(self, perfil_id: UUID) -> Perfil | None:
        return self.perfiles.get(perfil_id)

    async def por_cliente(self, cliente_id: UUID, limite: int) -> list[Perfil]:
        propios = [p for p in self.perfiles.values() if p.cliente_id == cliente_id]
        return sorted(propios, key=lambda p: p.calculado_en, reverse=True)[:limite]


class RelojFijo:
    def __init__(self, instante: datetime = AHORA) -> None:
        self.instante = instante

    def ahora(self) -> datetime:
        return self.instante


def verificacion(
    decision: DecisionConsentimiento = DecisionConsentimiento.PERMITIDO,
    permitidas: frozenset[Fuente] = frozenset(Fuente),
    causa: str | None = None,
) -> VerificacionConsentimiento:
    rechazado = decision is DecisionConsentimiento.RECHAZADO
    return VerificacionConsentimiento(
        decision=decision,
        consentimiento_id=None if rechazado else CONSENTIMIENTO_C01,
        vigente_hasta=None if rechazado else datetime(2027, 10, 1, tzinfo=UTC),
        alcances_permitidos=frozenset() if rechazado else permitidas,
        causa=causa,
    )


@dataclass
class Escenario:
    identidad: IdentidadFalsa
    consentimiento: ConsentimientoFalso
    modelos: ModelosFalsos
    ejecutor: EjecutorFalso
    repo: RepositorioPerfilesMemoria
    reloj: RelojFijo
    config: ConfigPerfilamiento
    # Comprobación final tras la fase B (HU-W31, ADR 0011 §3): vigente por omisión.
    comprobacion: ConsentimientoFalso = field(
        default_factory=lambda: ConsentimientoFalso(verificacion())
    )

    def caso(self) -> CrearPerfil:
        return CrearPerfil(
            identidad=self.identidad,
            consentimiento=self.consentimiento,
            modelos=self.modelos,
            plan_fuentes=plan_fuentes,
            ejecutor=self.ejecutor,
            repo=self.repo,
            reloj=self.reloj,
            config=self.config,
            comprobacion=self.comprobacion,
        )


@pytest.fixture
def escenario(modelo: ModeloRiesgo) -> Escenario:
    """C01 nominal: identidad verificada, 4 alcances y simulador nominal."""
    return Escenario(
        identidad=IdentidadFalsa(EstadoIdentidad.VERIFICADO),
        consentimiento=ConsentimientoFalso(verificacion()),
        modelos=ModelosFalsos(modelo),
        ejecutor=EjecutorFalso(),
        repo=RepositorioPerfilesMemoria(),
        reloj=RelojFijo(),
        config=ConfigPerfilamiento(dependencia_timeout_ms=150, fuente_timeout_ms=300),
    )


def comando(**cambios: Any) -> ComandoPerfil:
    base = ComandoPerfil(
        cliente_id=CLIENTE_C01,
        producto_id="vida-hipotecario",
        datos_declarados=DatosDeclarados(date(1990, 4, 18), 180, Decimal(180000000)),
        idempotency_key=UUID("00000000-0000-4000-b000-0000000000c1"),
        actor_id="asesor-sintetico",
        correlation_id="corr-ut",
    )
    return replace(base, **cambios)


@pytest.fixture
def nuevo_comando() -> Any:
    return comando


@pytest.fixture
def verificacion_de() -> Any:
    return verificacion
