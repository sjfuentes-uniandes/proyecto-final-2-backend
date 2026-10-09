import functools
import inspect
import ipaddress
import logging
import os
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Self, TypeVar

import httpx
from fastapi import FastAPI
from opentelemetry import context as otel_context
from opentelemetry import metrics, propagate, trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.propagate import get_global_textmap
from opentelemetry.propagators.aws import AwsXRayPropagator
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_ON,
    Decision,
    ParentBased,
    Sampler,
    SamplingResult,
)
from opentelemetry.trace import SpanKind, Status, StatusCode

from solventa_common import correlation
from solventa_common.logging import configurar_logging, operacion_actual
from solventa_common.settings import ServiceSettings

METRICA_DURACION = "OperationDuration"
METRICA_ERRORES = "OperationErrors"
ATRIBUTO_OPERACION = "Operation"

RESULTADO_OK = "ok"
RESULTADO_ERROR = "error"
RESULTADO_TIMEOUT = "timeout"
RESULTADO_RECHAZADO = "rechazado"

_ERRORES_TIMEOUT: tuple[type[BaseException], ...] = (TimeoutError, httpx.TimeoutException)
_RUTAS_SALUD = {"/health"}

logger = logging.getLogger("solventa.telemetria")
F = TypeVar("F", bound=Callable[..., Any])


# --- Estado de la telemetría del proceso -------------------------------------------
@dataclass
class _Telemetria:
    tracer: trace.Tracer
    duracion: metrics.Histogram
    errores: metrics.Counter
    tracer_provider: trace.TracerProvider
    meter_provider: metrics.MeterProvider


def _instrumentos(tracer_provider: trace.TracerProvider, meter_provider: metrics.MeterProvider) -> _Telemetria:
    meter = meter_provider.get_meter("solventa")
    return _Telemetria(
        tracer=tracer_provider.get_tracer("solventa"),
        duracion=meter.create_histogram(METRICA_DURACION, unit="ms", description="Duración de la operación"),
        errores=meter.create_counter(METRICA_ERRORES, unit="1", description="Operaciones con error o timeout"),
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
    )


# Sin configurar se usan los proveedores globales (no-op hasta que se llame a configurar).
_estado = _instrumentos(trace.get_tracer_provider(), metrics.get_meter_provider())


def _activo(variable: str) -> bool:
    return os.getenv(variable, "").strip().lower() == "true"


class _CorrelacionEnSpans(SpanProcessor):
    """Copia el correlationId del contexto a cada span (servidor, internos y clientes)."""

    def on_start(self, span: Span, parent_context: otel_context.Context | None = None) -> None:
        correlation_id = correlation.obtener()
        if correlation_id:
            span.set_attribute(correlation.ATRIBUTO_CORRELACION, correlation_id)

    def on_end(self, span: ReadableSpan) -> None:
        # Nada que hacer al cerrar: el atributo se fija al iniciar el span.
        return None


def _es_loopback(direccion: object) -> bool:
    try:
        return ipaddress.ip_address(str(direccion)).is_loopback
    except ValueError:
        return False


class _SinChequeoDeSalud(Sampler):
    """No traza el GET /health que el healthcheck de ECS hace desde 127.0.0.1 cada 10 s."""

    def __init__(self, delegado: Sampler) -> None:
        self._delegado = delegado

    def should_sample(self, parent_context, trace_id, name, kind=None, attributes=None, links=None, trace_state=None):
        atributos = attributes or {}
        ruta = atributos.get("url.path") or atributos.get("http.target")
        cliente = atributos.get("client.address") or atributos.get("net.peer.ip")
        if kind == SpanKind.SERVER and ruta in _RUTAS_SALUD and _es_loopback(cliente):
            return SamplingResult(Decision.DROP)
        return self._delegado.should_sample(parent_context, trace_id, name, kind, attributes, links, trace_state)

    def get_description(self) -> str:
        return "SinChequeoDeSalud"


def _propagador_xray() -> None:
    # OTEL_PROPAGATORS=tracecontext,baggage,xray ya lo incluye; en local se asegura.
    actual = get_global_textmap()
    if "X-Amzn-Trace-Id" not in actual.fields:
        propagate.set_global_textmap(CompositePropagator([actual, AwsXRayPropagator()]))


def configurar(
    settings: ServiceSettings,
    *,
    span_exporter: SpanExporter | None = None,
    metric_readers: Sequence[MetricReader] = (),
) -> None:
    """Configura trazas y métricas. Los exportadores explícitos se usan en pruebas."""
    global _estado
    if _activo("OTEL_SDK_DISABLED"):
        _estado = _instrumentos(trace.NoOpTracerProvider(), metrics.NoOpMeterProvider())
        return

    recurso = Resource.create({
        "service.name": settings.service_name,
        "deployment.environment": settings.environment,
        "Service": settings.service_name,
        "Environment": settings.environment,
    })
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")

    tracer_provider = TracerProvider(
        resource=recurso,
        id_generator=AwsXRayIdGenerator(),
        sampler=ParentBased(root=_SinChequeoDeSalud(ALWAYS_ON)),
    )
    tracer_provider.add_span_processor(_CorrelacionEnSpans())
    if span_exporter is not None:
        tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    elif endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True)))

    lectores = list(metric_readers)
    if not lectores and endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

        # La agregación exponencial llega por OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION.
        lectores.append(PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=endpoint, insecure=True)))
    vistas = [View(instrument_name=nombre.lower(), name=nombre) for nombre in (METRICA_DURACION, METRICA_ERRORES)]
    meter_provider = MeterProvider(resource=recurso, metric_readers=lectores, views=vistas)

    if isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
        trace.set_tracer_provider(tracer_provider)
    if type(metrics.get_meter_provider()).__name__ == "_ProxyMeterProvider":
        metrics.set_meter_provider(meter_provider)
    _propagador_xray()
    _estado = _instrumentos(tracer_provider, meter_provider)


def apagar() -> None:
    """Vacía los lotes pendientes al detener la tarea (SIGTERM de ECS)."""
    for proveedor in (_estado.tracer_provider, _estado.meter_provider):
        cerrar = getattr(proveedor, "shutdown", None)
        if callable(cerrar):
            cerrar()


# --- Operaciones medidas -----------------------------------------------------------
def _resultado_de(error: BaseException | None) -> str:
    if error is None:
        return RESULTADO_OK
    if isinstance(error, _ERRORES_TIMEOUT):
        return RESULTADO_TIMEOUT
    return RESULTADO_ERROR


def registrar(nombre: str, duracion_ms: float, resultado: str, **extra: Any) -> None:
    """Publica las métricas y el log de una operación ya medida."""
    atributos = {ATRIBUTO_OPERACION: nombre}
    _estado.duracion.record(duracion_ms, atributos)
    # Se suma 0 en los éxitos para que la serie exista y la tasa de error sea calculable.
    _estado.errores.add(1 if resultado in (RESULTADO_ERROR, RESULTADO_TIMEOUT) else 0, atributos)
    nivel = logging.ERROR if resultado in (RESULTADO_ERROR, RESULTADO_TIMEOUT) else logging.INFO
    logger.log(
        nivel,
        "operación %s: %s",
        nombre,
        resultado,
        extra={"operation": nombre, "result": resultado, "duration_ms": round(duracion_ms, 2), **extra},
    )


# Nombre en minúscula: se usa como función, with operacion("x") o @operacion("x").
class operacion:
    """Mide una operación de negocio: span, OperationDuration, OperationErrors y log."""

    def __init__(self, nombre: str) -> None:
        self.nombre = nombre
        self.resultado: str | None = None
        self._pila: list[tuple[Any, Any, float]] = []

    def marcar(self, resultado: str) -> None:
        """Fija un resultado sin excepción, por ejemplo RESULTADO_RECHAZADO."""
        self.resultado = resultado

    def __enter__(self) -> Self:
        span_cm = _estado.tracer.start_as_current_span(
            self.nombre,
            attributes={"operation": self.nombre},
            record_exception=False,
            set_status_on_exception=False,
        )
        span = span_cm.__enter__()
        token = operacion_actual.set(self.nombre)
        self._pila.append((span_cm, (span, token), time.perf_counter()))
        self.resultado = None
        return self

    def __exit__(self, tipo, error, tb) -> bool:
        span_cm, (span, token), inicio = self._pila.pop()
        duracion_ms = (time.perf_counter() - inicio) * 1000
        resultado = self.resultado or _resultado_de(error)
        span.set_attribute("result", resultado)
        extra: dict[str, Any] = {}
        if error is not None:
            # Solo el tipo: el mensaje puede contener datos del cliente (AC4).
            extra["error_type"] = type(error).__name__
            span.set_status(Status(StatusCode.ERROR, type(error).__name__))
        elif resultado in (RESULTADO_ERROR, RESULTADO_TIMEOUT):
            span.set_status(Status(StatusCode.ERROR, resultado))
        try:
            registrar(self.nombre, duracion_ms, resultado, **extra)
        finally:
            operacion_actual.reset(token)
            span_cm.__exit__(None, None, None)
        return False

    async def __aenter__(self) -> Self:
        return self.__enter__()

    async def __aexit__(self, tipo, error, tb) -> bool:
        return self.__exit__(tipo, error, tb)

    def __call__(self, funcion: F) -> F:
        if inspect.iscoroutinefunction(funcion):

            @functools.wraps(funcion)
            async def envoltura_async(*args: Any, **kwargs: Any) -> Any:
                with operacion(self.nombre):
                    return await funcion(*args, **kwargs)

            return envoltura_async  # type: ignore[return-value]

        @functools.wraps(funcion)
        def envoltura(*args: Any, **kwargs: Any) -> Any:
            with operacion(self.nombre):
                return funcion(*args, **kwargs)

        return envoltura  # type: ignore[return-value]


# --- Medición por solicitud HTTP ------------------------------------------------------
class MedicionSolicitudMiddleware:
    """Una métrica y un log por solicitud con Operation = "<MÉTODO> <plantilla de ruta>".

    Así cada endpoint aparece en el tablero aunque el servicio aún no use
    `operacion`. Se omite el healthcheck local de ECS.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cliente = (scope.get("client") or ("",))[0]
        if scope.get("path") in _RUTAS_SALUD and _es_loopback(cliente):
            await self.app(scope, receive, send)
            return

        estado = {"codigo": 500}

        async def enviar(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                estado["codigo"] = message["status"]
            await send(message)

        inicio = time.perf_counter()
        error: BaseException | None = None
        try:
            await self.app(scope, receive, enviar)
        except BaseException as exc:
            error = exc
            raise
        finally:
            ruta = getattr(scope.get("route"), "path", None) or "sin_ruta"
            nombre = f"{scope.get('method', 'GET')} {ruta}"
            codigo = estado["codigo"]
            if error is not None:
                resultado = _resultado_de(error)
            elif codigo >= 500:
                resultado = RESULTADO_ERROR
            elif codigo >= 400:
                resultado = RESULTADO_RECHAZADO
            else:
                resultado = RESULTADO_OK
            registrar(nombre, (time.perf_counter() - inicio) * 1000, resultado, status=codigo)


# --- Punto de entrada único -----------------------------------------------------------
def instrumentar(
    app: FastAPI,
    settings: ServiceSettings,
    *,
    span_exporter: SpanExporter | None = None,
    metric_readers: Sequence[MetricReader] = (),
) -> FastAPI:
    """Logs JSON, correlación, trazas y métricas para un servicio FastAPI."""
    configurar_logging(settings.service_name, settings.environment, os.getenv("LOG_LEVEL", "INFO"))
    configurar(settings, span_exporter=span_exporter, metric_readers=metric_readers)

    app.add_middleware(MedicionSolicitudMiddleware)
    if not _activo("OTEL_SDK_DISABLED"):
        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=_estado.tracer_provider,
            meter_provider=metrics.NoOpMeterProvider(),  # las métricas propias cubren el contrato
            exclude_spans=["receive", "send"],
        )
        httpx_instrumentor = HTTPXClientInstrumentor()
        if httpx_instrumentor.is_instrumented_by_opentelemetry:
            httpx_instrumentor.uninstrument()
        httpx_instrumentor.instrument(tracer_provider=_estado.tracer_provider, meter_provider=metrics.NoOpMeterProvider())
    # Correlación por fuera de todo (incluido el span del servidor y el manejador de 500):
    # Correlación -> OpenTelemetryMiddleware -> ServerErrorMiddleware -> Medición -> rutas.
    construir = app.build_middleware_stack

    def construir_con_correlacion() -> Any:
        return correlation.CorrelacionMiddleware(
            construir(),
            correlation_header=settings.correlation_header,
            request_id_header=settings.request_id_header,
        )

    app.build_middleware_stack = construir_con_correlacion  # type: ignore[method-assign]

    # Vacía trazas y métricas pendientes al terminar, respetando el lifespan del servicio.
    ciclo_original = app.router.lifespan_context

    @asynccontextmanager
    async def ciclo_con_apagado(aplicacion: Any) -> AsyncIterator[Any]:
        try:
            async with ciclo_original(aplicacion) as estado:
                yield estado
        finally:
            apagar()

    app.router.lifespan_context = ciclo_con_apagado
    return app
