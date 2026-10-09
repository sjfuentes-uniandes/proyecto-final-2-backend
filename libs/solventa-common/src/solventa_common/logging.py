import json
import logging
import re
import sys
from collections.abc import Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace

from solventa_common import correlation

REDACTADO = "[REDACTADO]"
MAX_TEXTO = 512
MAX_ELEMENTOS = 50
MAX_PROFUNDIDAD = 8

# Fragmentos de clave (en minúsculas, sin separadores) que nunca se registran.
_FRAGMENTOS_SENSIBLES = (
    "password", "passwd", "contrasena", "contraseña", "secret", "secreto", "token",
    "authorization", "autorizacion", "apikey", "cookie", "credential", "credencial",
    "privatekey", "llaveprivada", "selfie", "biometr", "imagen", "image", "foto",
    "cuenta", "ingreso", "obligacion", "salario", "saldo", "tarjeta", "card",
)
_CLAVES_SENSIBLES = {"pin", "cvv", "otp", "jwt"}

_MASCARAS = (
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer " + REDACTADO),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"), REDACTADO),
    (
        re.compile(
            r"(?i)\b(password|passwd|secret|client_secret|token|access_token|refresh_token|"
            r"api[_-]?key|authorization)(\"?\s*[=:]\s*\"?)[^\s\"',;&]+"
        ),
        r"\1\2" + REDACTADO,
    ),
)

# Operación y resultado en curso; los fija telemetry.operacion para los logs internos.
operacion_actual: ContextVar[str | None] = ContextVar("operacion_actual", default=None)

_CAMPOS_ESTANDAR = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message", "asctime", "taskName",
    "color_message",  # copia con códigos ANSI que agrega uvicorn
}
_CAMPOS_BASE = ("operation", "result", "duration_ms")


def es_clave_sensible(clave: str) -> bool:
    normalizada = re.sub(r"[\W_]", "", str(clave).lower())
    return normalizada in _CLAVES_SENSIBLES or any(f in normalizada for f in _FRAGMENTOS_SENSIBLES)


def enmascarar(texto: str, maximo: int = MAX_TEXTO) -> str:
    for patron, reemplazo in _MASCARAS:
        texto = patron.sub(reemplazo, texto)
    if len(texto) > maximo:
        texto = f"{texto[:maximo]}…[truncado {len(texto) - maximo} caracteres]"
    return texto


def sanitizar(valor: Any, _profundidad: int = 0) -> Any:
    """Copia segura para logs: redacta claves sensibles en cualquier nivel y trunca textos."""
    if _profundidad > MAX_PROFUNDIDAD:
        return "[profundidad máxima]"
    if valor is None or isinstance(valor, bool | int | float):
        return valor
    if isinstance(valor, str):
        return enmascarar(valor)
    if isinstance(valor, bytes | bytearray | memoryview):
        return f"[binario {len(valor)} bytes]"
    if hasattr(valor, "model_dump"):
        valor = valor.model_dump()
    if isinstance(valor, Mapping):
        return {
            str(k): REDACTADO if es_clave_sensible(k) else sanitizar(v, _profundidad + 1)
            for k, v in valor.items()
        }
    if isinstance(valor, list | tuple | set | frozenset):
        elementos = list(valor)
        resultado = [sanitizar(v, _profundidad + 1) for v in elementos[:MAX_ELEMENTOS]]
        if len(elementos) > MAX_ELEMENTOS:
            resultado.append(f"[{len(elementos) - MAX_ELEMENTOS} elementos omitidos]")
        return resultado
    return enmascarar(str(valor))


def trace_id_xray(trace_id: int) -> str:
    hexa = f"{trace_id:032x}"
    return f"1-{hexa[:8]}-{hexa[8:]}"


class FormateadorJson(logging.Formatter):
    def __init__(self, servicio: str, ambiente: str) -> None:
        super().__init__()
        self.servicio = servicio
        self.ambiente = ambiente

    def format(self, record: logging.LogRecord) -> str:
        contexto = trace.get_current_span().get_span_context()
        linea: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self.servicio,
            "environment": self.ambiente,
            "logger": record.name,
            "message": enmascarar(record.getMessage(), maximo=2 * MAX_TEXTO),
            "operation": getattr(record, "operation", None) or operacion_actual.get(),
            "result": getattr(record, "result", None),
            "duration_ms": getattr(record, "duration_ms", None),
            "correlation_id": correlation.obtener(),
            "trace_id": trace_id_xray(contexto.trace_id) if contexto.is_valid else None,
            "span_id": f"{contexto.span_id:016x}" if contexto.is_valid else None,
        }
        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in _CAMPOS_ESTANDAR and k not in _CAMPOS_BASE and not k.startswith("_")
        }
        linea.update(sanitizar(extras))
        if record.exc_info and record.exc_info[0] is not None:
            linea["error_type"] = record.exc_info[0].__name__
            linea["exception"] = enmascarar(self.formatException(record.exc_info), maximo=4 * MAX_TEXTO)
        return json.dumps(linea, ensure_ascii=False, default=str)


def configurar_logging(servicio: str, ambiente: str, nivel: str = "INFO") -> None:
    """Un único handler JSON a stdout (awslogs lo envía a /ecs/<prefijo>/<servicio>)."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(FormateadorJson(servicio, ambiente))
    raiz = logging.getLogger()
    for anterior in list(raiz.handlers):
        raiz.removeHandler(anterior)
    raiz.addHandler(handler)
    raiz.setLevel(nivel)
    # Uvicorn configura sus loggers antes de importar la app: se reenvían al raíz.
    for nombre in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(nombre)
        logger.handlers.clear()
        logger.propagate = True
    # El access log de uvicorn y los INFO de httpx incluyen URLs con query string; el
    # log por solicitud y los spans de cliente los reemplazan.
    logging.getLogger("uvicorn.access").disabled = True
    for nombre in ("httpx", "httpcore"):
        logging.getLogger(nombre).setLevel(logging.WARNING)
