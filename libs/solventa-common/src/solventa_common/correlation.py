import re
import uuid
from collections.abc import Awaitable, Callable, MutableMapping
from contextvars import ContextVar, Token
from typing import Any

import httpx
from opentelemetry import propagate, trace

CORRELATION_HEADER = "X-Correlation-Id"
REQUEST_ID_HEADER = "X-Request-Id"
# Atributo de los spans; el colector lo indexa como anotación de X-Ray.
ATRIBUTO_CORRELACION = "correlation_id"

_PATRON = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def es_valido(valor: str | None) -> bool:
    return valor is not None and _PATRON.fullmatch(valor) is not None


def resolver(correlation_id: str | None, request_id: str | None) -> str:
    if es_valido(correlation_id):
        return correlation_id  # type: ignore[return-value]
    if es_valido(request_id):
        return request_id  # type: ignore[return-value]
    return str(uuid.uuid4())


def obtener() -> str | None:
    return _correlation_id.get()


def establecer(valor: str) -> Token[str | None]:
    return _correlation_id.set(valor)


def restablecer(token: Token[str | None]) -> None:
    _correlation_id.reset(token)


class CorrelacionMiddleware:
    """Middleware ASGI: resuelve el ID, lo guarda en contexto y lo devuelve en la respuesta."""

    def __init__(
        self,
        app: ASGIApp,
        correlation_header: str = CORRELATION_HEADER,
        request_id_header: str = REQUEST_ID_HEADER,
    ) -> None:
        self.app = app
        self.correlation_header = correlation_header
        self._correlation_key = correlation_header.lower().encode("latin-1")
        self._request_id_key = request_id_header.lower().encode("latin-1")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        recibidos = {}
        for clave, valor in scope.get("headers", []):
            if clave in (self._correlation_key, self._request_id_key):
                recibidos.setdefault(clave, valor.decode("latin-1"))
        correlation_id = resolver(recibidos.get(self._correlation_key), recibidos.get(self._request_id_key))
        # Si ya hay un span activo (middleware montado dentro del de OpenTelemetry) se anota;
        # con instrumentar() lo hace el procesador de spans al crearlos.
        trace.get_current_span().set_attribute(ATRIBUTO_CORRELACION, correlation_id)

        async def enviar(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != self._correlation_key]
                headers.append((self.correlation_header.encode("latin-1"), correlation_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        token = establecer(correlation_id)
        try:
            await self.app(scope, receive, enviar)
        finally:
            restablecer(token)


def encabezados_salientes(headers: MutableMapping[str, str] | None = None) -> MutableMapping[str, str]:
    """Agrega el correlationId y el contexto de traza (traceparent, X-Amzn-Trace-Id)."""
    headers = {} if headers is None else headers
    correlation_id = obtener()
    if correlation_id:
        headers[CORRELATION_HEADER] = correlation_id
    propagate.inject(headers)
    return headers


def propagar(request: httpx.Request) -> None:
    """Hook de solicitud para httpx.Client."""
    correlation_id = obtener()
    if correlation_id:
        request.headers[CORRELATION_HEADER] = correlation_id
    # Si la instrumentación de httpx está activa, reemplaza esto con el span del cliente.
    propagate.inject(request.headers)


async def propagar_async(request: httpx.Request) -> None:
    """Hook de solicitud para httpx.AsyncClient."""
    propagar(request)


def _con_hook(kwargs: dict[str, Any], hook: Callable[[httpx.Request], Any]) -> dict[str, Any]:
    hooks = dict(kwargs.pop("event_hooks", None) or {})
    hooks["request"] = [hook, *hooks.get("request", [])]
    return {**kwargs, "event_hooks": hooks}


def cliente_async(**kwargs: Any) -> httpx.AsyncClient:
    """httpx.AsyncClient que propaga el correlationId y la traza a otro servicio."""
    return httpx.AsyncClient(**_con_hook(kwargs, propagar_async))


def cliente(**kwargs: Any) -> httpx.Client:
    """httpx.Client que propaga el correlationId y la traza a otro servicio."""
    return httpx.Client(**_con_hook(kwargs, propagar))
