"""Continuidad multizona (HU-W29): estado por zona, base principal, recorridos críticos,
cronología del incidente e indicadores del experimento.

Fuentes (todas de "Métricas, trazas, registros y alertas", modelo_despliegue.puml):
- eventos de ECS, RDS y AWS FIS que EventBridge guarda en el grupo de logs de la cronología
  (infra/platform/observability.tf; `ECS ..> Observability` y `RDS ..> Observability` en
  diagrama_despliegue_ecs_fargate.puml);
- tráfico del ALB interno por zona y alarmas de HU-W28 (CloudWatch);
- informe de reconciliar.py (experiments/hu-w29) en el bucket de experimentos de exp.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

ESTADO_ALARMA, ESTADO_OK = "ALARM", "OK"
# Eventos de RDS de un failover Multi-AZ (RDS User Guide, categoría failover).
FAILOVER_INICIADO = frozenset({"RDS-EVENT-0013"})
FAILOVER_COMPLETADO = frozenset({"RDS-EVENT-0015", "RDS-EVENT-0049"})
FIS_ACTIVO = frozenset({"initiating", "running"})
FIS_TERMINADO = frozenset({"completed", "stopped", "failed", "cancelled"})
MAX_HITOS = 100


@dataclass(frozen=True)
class EstadoTarea:
    """Último estado conocido de una tarea ECS (ECS Task State Change)."""

    zona: str
    estado: str
    deseado: str
    salud: str = "UNKNOWN"

    @property
    def esperada(self) -> bool:
        return self.deseado == "RUNNING"

    @property
    def saludable(self) -> bool:
        return self.esperada and self.estado == "RUNNING" and self.salud != "UNHEALTHY"


@dataclass(frozen=True)
class Zona:
    zona: str
    tareas_saludables: int
    tareas_esperadas: int
    trafico_pct: float | None
    estado: str  # SALUDABLE, DEGRADADA, RETIRADA o NO_DISPONIBLE


@dataclass(frozen=True)
class Base:
    estado: str  # MULTI_AZ, FAILOVER_EN_CURSO o STANDBY_PROMOVIDO
    desde: datetime | None = None


@dataclass(frozen=True)
class Recorrido:
    id: str
    nombre: str
    estado: str  # OPERANDO o DEGRADADO
    alarmas_abiertas: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Hito:
    instante: datetime
    fuente: str  # FIS, ECS, RDS o ALARMA
    tipo: str
    descripcion: str
    zona: str | None = None
    cantidad: int | None = None


@dataclass(frozen=True)
class Indicadores:
    experimento: str
    estado_conciliacion: str
    transacciones_perdidas: int
    cobros_duplicados: int
    redireccion_s: int | None
    objetivo_redireccion_s: int | None
    porcentaje_error: float


@dataclass(frozen=True)
class Continuidad:
    estado: str  # DOS_ZONAS, UNA_ZONA o SIN_DATOS
    zonas: list[Zona]
    base: Base
    recorridos: list[Recorrido]
    cronologia: list[Hito]
    incidente_desde: datetime | None
    indicadores: Indicadores | None


def zonas(tareas: Iterable[EstadoTarea], trafico: Mapping[str, float]) -> list[Zona]:
    """Tareas saludables frente a esperadas y parte del tráfico del ALB por zona.

    RETIRADA: la zona tiene tareas, pero el balanceador ya no le entrega tráfico.
    """
    por_zona: dict[str, list[EstadoTarea]] = {}
    for tarea in tareas:
        if tarea.zona:
            por_zona.setdefault(tarea.zona, []).append(tarea)
    for nombre in trafico:
        por_zona.setdefault(nombre, [])
    total = sum(trafico.values())
    resultado = []
    for nombre, propias in sorted(por_zona.items()):
        saludables = sum(1 for t in propias if t.saludable)
        esperadas = sum(1 for t in propias if t.esperada)
        parte = round(100 * trafico.get(nombre, 0) / total, 1) if total else None
        if saludables == 0:
            estado = "NO_DISPONIBLE"
        elif parte == 0:
            estado = "RETIRADA"
        elif saludables < esperadas:
            estado = "DEGRADADA"
        else:
            estado = "SALUDABLE"
        resultado.append(Zona(nombre, saludables, esperadas, parte, estado))
    return resultado


def estado_general(lista: list[Zona]) -> str:
    operando = sum(1 for z in lista if z.estado in ("SALUDABLE", "DEGRADADA"))
    if operando >= 2:
        return "DOS_ZONAS"
    return "UNA_ZONA" if operando == 1 else "SIN_DATOS"


def _instante(evento: Mapping[str, Any]) -> datetime:
    return datetime.fromisoformat(str(evento["time"]))


def base(eventos: Iterable[Mapping[str, Any]]) -> Base:
    """Estado de la base principal según el último evento de failover de RDS."""
    ultimo: Base = Base("MULTI_AZ")
    for evento in sorted(_de(eventos, "RDS DB Instance Event"), key=_instante):
        id_evento = evento.get("detail", {}).get("EventID")
        if id_evento in FAILOVER_INICIADO:
            ultimo = Base("FAILOVER_EN_CURSO", _instante(evento))
        elif id_evento in FAILOVER_COMPLETADO:
            ultimo = Base("STANDBY_PROMOVIDO", _instante(evento))
    return ultimo


def recorridos(
    configurados: Mapping[str, Mapping[str, Any]], abiertas: Iterable[str], prefijo: str
) -> list[Recorrido]:
    """Un recorrido está degradado si alguna de sus alarmas de HU-W28 está en ALARM."""
    en_alarma = {a.removeprefix(prefijo) for a in abiertas}
    resultado = []
    for id_, config in sorted(configurados.items()):
        propias = [a for a in config.get("alarmas", []) if a in en_alarma]
        estado = "DEGRADADO" if propias else "OPERANDO"
        resultado.append(Recorrido(id_, str(config.get("nombre", id_)), estado, propias))
    return resultado


def _de(eventos: Iterable[Mapping[str, Any]], tipo: str) -> list[Mapping[str, Any]]:
    return [e for e in eventos if e.get("detail-type") == tipo]


def _hitos_fis(eventos: Iterable[Mapping[str, Any]]) -> list[Hito]:
    hitos = []
    for evento in _de(eventos, "FIS Experiment State Change"):
        detalle = evento.get("detail", {})
        estado = str((detalle.get("new-state") or {}).get("status", "")).lower()
        experimento = str(detalle.get("experiment-id", ""))
        if estado == "running":
            hitos.append(
                Hito(
                    _instante(evento),
                    "FIS",
                    "INYECCION",
                    f"Inicio de la falla controlada {experimento}",
                )
            )
        elif estado in FIS_TERMINADO:
            hitos.append(
                Hito(
                    _instante(evento),
                    "FIS",
                    "FIN_INYECCION",
                    f"Fin de la falla controlada ({estado})",
                )
            )
    return hitos


def _hitos_ecs(eventos: Iterable[Mapping[str, Any]]) -> list[Hito]:
    """Tareas detenidas e iniciadas por minuto y zona (cada tarea una vez por estado). El hito
    lleva la hora del primer evento del grupo, no la del inicio del minuto."""
    grupos: dict[tuple[str, datetime, str], tuple[datetime, set[str]]] = {}
    for evento in _de(eventos, "ECS Task State Change"):
        detalle = evento.get("detail", {})
        if detalle.get("lastStatus") == "STOPPED":
            tipo = "TAREAS_DETENIDAS"
        elif detalle.get("lastStatus") == "RUNNING" and detalle.get("desiredStatus") == "RUNNING":
            tipo = "TAREAS_INICIADAS"
        else:
            continue
        instante = _instante(evento)
        clave = (
            tipo,
            instante.replace(second=0, microsecond=0),
            str(detalle.get("availabilityZone", "")),
        )
        primero, tareas = grupos.get(clave, (instante, set()))
        tareas.add(str(detalle.get("taskArn", "")))
        grupos[clave] = (min(primero, instante), tareas)
    descripciones = {
        "TAREAS_DETENIDAS": "tareas retiradas",
        "TAREAS_INICIADAS": "tareas en ejecución",
    }
    return [
        Hito(
            primero,
            "ECS",
            tipo,
            f"{len(tareas)} {descripciones[tipo]} en {zona}",
            zona or None,
            len(tareas),
        )
        for (tipo, _, zona), (primero, tareas) in grupos.items()
    ]


def _hitos_rds(eventos: Iterable[Mapping[str, Any]]) -> list[Hito]:
    hitos = []
    for evento in _de(eventos, "RDS DB Instance Event"):
        detalle = evento.get("detail", {})
        id_evento = detalle.get("EventID")
        tipo = "EVENTO_BASE"
        if id_evento in FAILOVER_INICIADO:
            tipo = "FAILOVER_INICIADO"
        elif id_evento in FAILOVER_COMPLETADO:
            tipo = "FAILOVER_COMPLETADO"
        hitos.append(
            Hito(_instante(evento), "RDS", tipo, str(detalle.get("Message", id_evento or "")))
        )
    return hitos


def _hitos_alarmas(
    transiciones: Iterable[Any], titulos: Mapping[str, str], prefijo: str
) -> list[Hito]:
    hitos = []
    for t in transiciones:
        nombre = t.alarma.removeprefix(prefijo)
        titulo = titulos.get(nombre, nombre)
        if t.nuevo == ESTADO_ALARMA:
            hitos.append(Hito(t.momento, "ALARMA", "ALARMA", titulo))
        elif t.nuevo == ESTADO_OK and t.anterior == ESTADO_ALARMA:
            hitos.append(Hito(t.momento, "ALARMA", "RECUPERADA", titulo))
    return hitos


def cronologia(
    eventos: list[Mapping[str, Any]],
    transiciones: Iterable[Any],
    titulos: Mapping[str, str],
    prefijo: str,
) -> list[Hito]:
    """Inyección, detección (alarmas), retiro y reposición de tareas, failover y recuperación."""
    hitos = [
        *_hitos_fis(eventos),
        *_hitos_ecs(eventos),
        *_hitos_rds(eventos),
        *_hitos_alarmas(transiciones, titulos, prefijo),
    ]
    return sorted(hitos, key=lambda h: (h.instante, h.fuente))[-MAX_HITOS:]


def inicio_incidente(hitos: list[Hito]) -> datetime | None:
    """La inyección de FIS; sin experimento, la primera alarma de la ventana."""
    for tipo in ("INYECCION", "ALARMA"):
        primero = next((h.instante for h in hitos if h.tipo == tipo), None)
        if primero:
            return primero
    return None


def indicadores(informe: Mapping[str, Any] | None) -> Indicadores | None:
    """Indicadores de I04 desde el informe de reconciliar.py (versión 1)."""
    if not informe or informe.get("version") != 1:
        return None
    resumen = informe.get("resumen", {})
    continuidad = informe.get("continuidad") or {}
    return Indicadores(
        experimento=str(informe.get("experimento", "")),
        estado_conciliacion=str(informe.get("estado", "")),
        transacciones_perdidas=int(resumen.get("perdidas", 0)),
        cobros_duplicados=int(resumen.get("cobrosDuplicados", 0)),
        redireccion_s=continuidad.get("redireccionSegundos"),
        objetivo_redireccion_s=continuidad.get("objetivoSegundos"),
        porcentaje_error=float(resumen.get("porcentajeError", 0.0)),
    )
