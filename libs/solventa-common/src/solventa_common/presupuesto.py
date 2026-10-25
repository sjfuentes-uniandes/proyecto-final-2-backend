"""Presupuesto de tiempo de un recorrido (HU-W05, DT-10).

El recorrido fija un *deadline* al entrar (`iniciar`) y cada llamada usa el tiempo restante
(`restante_ms`) como `deadline_ms` de `resiliencia.timeout_efectivo_ms`. Entre servicios viaja en
`X-Deadline-Ms` como **restante relativo** en ms y no como instante absoluto: así no depende de la
sincronía de relojes entre tareas, y el salto de red solo puede acortarlo (quien llama corta con
ese mismo valor).

- Quien recibe el encabezado usa min(su presupuesto, el recibido).
- Un valor inválido (no entero o < 1) se ignora: el servicio sigue con su propio presupuesto.
- `LATENCY_BUDGET_MS` (JSON operación → ms) llega de `latency_budget_ms` en `apps.tfvars`.
"""

import json
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from opentelemetry import trace

ENCABEZADO = "X-Deadline-Ms"
ATRIBUTO_PRESUPUESTO = "solventa.presupuesto_ms"
ATRIBUTO_RESTANTE = "solventa.restante_ms"
ATRIBUTO_TIMEOUT = "solventa.timeout_ms"


@dataclass(frozen=True, slots=True)
class Presupuesto:
    total_ms: int
    fin_s: float

    def restante_ms(self) -> int:
        return max(0, int((self.fin_s - time.monotonic()) * 1000))


_actual: ContextVar[Presupuesto | None] = ContextVar("presupuesto", default=None)


def actual() -> Presupuesto | None:
    return _actual.get()


def restante_ms() -> int | None:
    """Tiempo restante del recorrido en curso; None fuera de un recorrido con presupuesto."""
    presupuesto = _actual.get()
    return None if presupuesto is None else presupuesto.restante_ms()


@contextmanager
def iniciar(propio_ms: int, recibido_ms: int | None = None) -> Iterator[Presupuesto]:
    """Abre el presupuesto del recorrido: min(propio, recibido en X-Deadline-Ms).

    Anota `solventa.presupuesto_ms` en el span actual (el de la operación del recorrido).
    """
    total = propio_ms if recibido_ms is None else min(propio_ms, recibido_ms)
    presupuesto = Presupuesto(total, time.monotonic() + total / 1000)
    trace.get_current_span().set_attribute(ATRIBUTO_PRESUPUESTO, total)
    token = _actual.set(presupuesto)
    try:
        yield presupuesto
    finally:
        _actual.reset(token)


def leer_encabezado(valor: str | None) -> int | None:
    """X-Deadline-Ms como entero positivo; None si falta o es inválido."""
    if valor is None:
        return None
    try:
        ms = int(valor.strip())
    except ValueError:
        return None
    return ms if ms >= 1 else None


def encabezado(ms: float) -> dict[str, str]:
    """El encabezado a enviar con `ms` restantes (nunca menor que 1)."""
    return {ENCABEZADO: str(max(1, int(ms)))}


def por_operacion(valor: str | Mapping[str, int] | None) -> dict[str, int]:
    """LATENCY_BUDGET_MS (JSON) como dict; vacío si no se configuró."""
    if valor is None or valor == "":
        return {}
    datos = json.loads(valor) if isinstance(valor, str) else dict(valor)
    if not isinstance(datos, dict) or not all(
        isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in datos.values()
    ):
        raise ValueError("LATENCY_BUDGET_MS debe ser un objeto operación → ms positivos")
    return {str(k): v for k, v in datos.items()}


def validar_timeouts(operacion: str, presupuesto_ms: int, timeouts_ms: Mapping[str, int]) -> None:
    """Cada dependencia debe tener un timeout **menor** que el presupuesto del recorrido.

    Se llama al construir la configuración: si falla, el servicio no arranca.
    """
    excedidos = {k: v for k, v in timeouts_ms.items() if v >= presupuesto_ms}
    if excedidos:
        detalle = ", ".join(f"{k}={v}" for k, v in sorted(excedidos.items()))
        raise ValueError(f"timeouts ≥ presupuesto de {operacion} ({presupuesto_ms} ms): {detalle}")
