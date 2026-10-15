"""Cuota por socio (HU-W02): API Gateway la aplica a cada API key; aquí se calcula su consumo."""

from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum

from api_socios.domain.socios import Plan

UMBRAL_CERCA_DEL_LIMITE = 80


class PlanNoDisponible(LookupError):
    pass


class Periodo(StrEnum):
    DIA = "DAY"
    SEMANA = "WEEK"
    MES = "MONTH"


class EstadoCuota(StrEnum):
    NORMAL = "normal"
    CERCA_DEL_LIMITE = "cerca_del_limite"
    AGOTADA = "agotada"


def plan_de_uso(planes: dict[str, str], plan: Plan) -> str:
    try:
        return planes[plan.value]
    except KeyError as error:
        raise PlanNoDisponible(f"El plan {plan.value} no está en PARTNERS_USAGE_PLANS") from error


def planes_disponibles(planes: dict[str, str]) -> list[Plan]:
    return [plan for plan in Plan if plan.value in planes]


@dataclass(frozen=True)
class LimitesPlan:
    plan: Plan
    limite_por_segundo: float
    rafaga: int
    cuota: int
    periodo: Periodo
    desplazamiento: int = 0


@dataclass(frozen=True)
class Ventana:
    """Días UTC, ambos inclusive, del periodo de cuota vigente."""

    inicio: date
    fin: date

    @property
    def reinicio(self) -> datetime:
        return datetime.combine(self.fin + timedelta(days=1), time(), UTC)


def ventana(periodo: Periodo, hoy: date, desplazamiento: int = 0) -> Ventana:
    """El desplazamiento sigue a API Gateway: en WEEK 0 es domingo; en MONTH 0 es el día 1."""
    if periodo is Periodo.DIA:
        return Ventana(hoy, hoy)
    if periodo is Periodo.SEMANA:
        desde_domingo = (hoy.weekday() + 1) % 7
        inicio = hoy - timedelta(days=(desde_domingo - desplazamiento) % 7)
        return Ventana(inicio, inicio + timedelta(days=6))
    dia = desplazamiento + 1
    inicio = date(hoy.year, hoy.month, dia)
    if hoy.day < dia:
        anterior = inicio.replace(day=1) - timedelta(days=1)
        inicio = date(anterior.year, anterior.month, dia)
    siguiente_mes = inicio + timedelta(days=monthrange(inicio.year, inicio.month)[1])
    return Ventana(inicio, siguiente_mes - timedelta(days=1))


@dataclass(frozen=True)
class Consumo:
    limites: LimitesPlan
    ventana: Ventana
    usado: int
    restante: int
    limitadas: int | None = None

    @property
    def porcentaje(self) -> int:
        if self.limites.cuota <= 0:
            return 100
        return min(100, self.usado * 100 // self.limites.cuota)

    @property
    def estado(self) -> EstadoCuota:
        if self.restante <= 0:
            return EstadoCuota.AGOTADA
        if self.porcentaje >= UMBRAL_CERCA_DEL_LIMITE:
            return EstadoCuota.CERCA_DEL_LIMITE
        return EstadoCuota.NORMAL


def calcular_consumo(
    limites: LimitesPlan,
    vigente: Ventana,
    registros: dict[str, list[list[int]]],
    api_key_id: str,
    limitadas: int | None = None,
) -> Consumo:
    """registros es items de GetUsage: por API key, un [usado, restante] por día de la ventana.

    Solo cuentan los registros de la API key del socio, aunque la respuesta traiga otras.
    """
    propios = registros.get(api_key_id) or []
    usado = sum(max(0, dia[0]) for dia in propios)
    restante = propios[-1][1] if propios else limites.cuota - usado
    return Consumo(limites, vigente, usado, max(0, restante), limitadas)
