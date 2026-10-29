"""Decisión de fallback de la cotización (HU-W06). Pura: sin reloj, sin E/S y sin float.

Una fuente de datos del perfil que no se obtuvo (tiempo agotado, caída o respuesta inválida) solo
permite una oferta si una regla de fallback **vigente** de catalogo lo dice. La matriz completa está
en docs/hu-w06-fallback.md:

- Fuente obligatoria (aporta un campo obligatorio del modelo de riesgo: el motor la lista en
  `datos_requeridos`) → PENDIENTE, aunque una regla diga otra cosa.
- Fuente opcional con regla DEGRADAR vigente → DEGRADADA: oferta provisional; el dato queda ausente
  y se informa en `faltantes`. Si la regla trae un ajuste, va como un factor más del desglose.
- Fuente opcional sin regla vigente, con regla PENDIENTE, o reglas vencidas o futuras → PENDIENTE.
- Varias fuentes: basta una PENDIENTE para que no haya oferta; todas se informan.

Nunca se inventa un valor: la decisión solo nombra la fuente, el dato y el motivo.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from cotizacion.domain.cotizacion.modelos import FactorAplicado
from cotizacion.domain.perfilamiento.errores import DependenciaNoDisponible
from cotizacion.domain.perfilamiento.modelos import EstadoFuente, Perfil

# Código del factor del desglose cuando una regla de fallback ajusta el precio.
FACTOR_FALLBACK = "FALLBACK"


class AccionFallback(StrEnum):
    """DEGRADAR: oferta provisional sin el dato. PENDIENTE: sin oferta, estado recuperable."""

    DEGRADAR = "DEGRADAR"
    PENDIENTE = "PENDIENTE"


class MotivoFalla(StrEnum):
    """Por qué no se obtuvo la fuente (estado del linaje del perfil)."""

    TIEMPO_AGOTADO = "TIEMPO_AGOTADO"
    NO_DISPONIBLE = "NO_DISPONIBLE"
    RESPUESTA_INVALIDA = "RESPUESTA_INVALIDA"


class TipoDecision(StrEnum):
    DEGRADADA = "DEGRADADA"
    PENDIENTE = "PENDIENTE"


class AccionSugerida(StrEnum):
    """Con PENDIENTE: reintentar con el mismo perfil no cambia nada; hay que recalcularlo (HU-W12)."""

    RECALCULAR_PERFIL = "RECALCULAR_PERFIL"


class MensajeFallback(StrEnum):
    """Clave del texto que muestran el portal y el socio; nunca un texto libre."""

    FUENTE_OPCIONAL_NO_DISPONIBLE = "FUENTE_OPCIONAL_NO_DISPONIBLE"
    FUENTE_REQUERIDA_NO_DISPONIBLE = "FUENTE_REQUERIDA_NO_DISPONIBLE"


@dataclass(frozen=True, slots=True)
class ReglaFallback:
    id: str
    fuente: str
    obligatoria: bool
    accion: AccionFallback
    # Factor sobre la prima ajustada; None = sin ajuste.
    ajuste: Decimal | None
    mensaje_codigo: MensajeFallback


@dataclass(frozen=True, slots=True)
class ReglasFallback:
    producto_id: str
    version: str
    hash: str
    vigente_desde: datetime
    # Vigencia semiabierta [vigente_desde, vigente_hasta); None = sin fin.
    vigente_hasta: datetime | None
    reglas: tuple[ReglaFallback, ...]

    def vigentes_en(self, instante: datetime) -> bool:
        return self.vigente_desde <= instante and (
            self.vigente_hasta is None or instante < self.vigente_hasta
        )


@dataclass(frozen=True, slots=True)
class FuenteFallida:
    """Una fuente del perfil que no se obtuvo, con los datos que debía aportar."""

    fuente: str
    motivo: MotivoFalla
    obligatoria: bool
    datos: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReglaAplicada:
    id: str
    version: str
    accion: AccionFallback


@dataclass(frozen=True, slots=True)
class Faltante:
    """Un dato que no se obtuvo. `regla` es None si la fuente no tenía regla vigente."""

    fuente: str
    dato: str
    motivo: MotivoFalla
    obligatoria: bool
    regla: ReglaAplicada | None


@dataclass(frozen=True, slots=True)
class DecisionFallback:
    tipo: TipoDecision
    faltantes: tuple[Faltante, ...]
    mensaje_codigo: MensajeFallback
    # Versión y huella de las reglas consultadas; None si no había reglas vigentes.
    version_reglas: str | None
    hash_reglas: str | None
    # Factores del desglose (solo con DEGRADADA y una regla con ajuste).
    ajustes: tuple[FactorAplicado, ...] = ()

    @property
    def accion_sugerida(self) -> AccionSugerida | None:
        return AccionSugerida.RECALCULAR_PERFIL if self.tipo is TipoDecision.PENDIENTE else None

    @property
    def fuentes(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(f.fuente for f in self.faltantes))


@dataclass(frozen=True, slots=True)
class DecisionRegistrada:
    """La decisión con lo que la identifica para la auditoría (AC3). Sin datos personales."""

    id: UUID
    decision: DecisionFallback
    perfil_id: UUID
    producto_id: str
    cotizacion_id: UUID | None
    actor_id: str
    idempotency_key: UUID
    huella_solicitud: str
    correlation_id: str
    decidido_en: datetime


# detalles.dependencia del 503: las fuentes del perfil (no una dependencia del recorrido).
DEPENDENCIA_FUENTES = "perfil.fuentes"


class CotizacionPendiente(DependenciaNoDisponible):
    """Una fuente del perfil faltó y ninguna regla permite degradar: no hay oferta definitiva.

    Es una DependenciaNoDisponible (503 DEPENDENCIA_NO_DISPONIBLE, recuperable, como HU-W04 y
    HU-W05); la API agrega la decisión registrada en los detalles.
    """

    def __init__(self, registro: DecisionRegistrada) -> None:
        super().__init__(DEPENDENCIA_FUENTES)
        self.registro = registro


_NO_OBTENIDA = {
    EstadoFuente.TIEMPO_AGOTADO: MotivoFalla.TIEMPO_AGOTADO,
    EstadoFuente.NO_DISPONIBLE: MotivoFalla.NO_DISPONIBLE,
    EstadoFuente.RESPUESTA_INVALIDA: MotivoFalla.RESPUESTA_INVALIDA,
}


def fuentes_fallidas(perfil: Perfil) -> tuple[FuenteFallida, ...]:
    """Las fuentes del linaje que no se obtuvieron. Una es obligatoria si el motor la listó en
    `datos_requeridos` (aporta un campo obligatorio del modelo con el que se calculó el perfil).

    NO_AUTORIZADA y NO_CONSULTADA no son fallas de la fuente (consentimiento): no entran.
    """
    requeridas = {d.fuente for d in perfil.resultado.datos_requeridos if d.fuente is not None}
    return tuple(
        FuenteFallida(
            fuente=r.fuente.value,
            motivo=_NO_OBTENIDA[r.estado],
            obligatoria=r.fuente in requeridas,
            datos=r.campos_solicitados,
        )
        for r in perfil.fuentes
        if r.estado in _NO_OBTENIDA
    )


def decidir_fallback(
    dependencias: Sequence[FuenteFallida], reglas: ReglasFallback | None, ahora: datetime
) -> DecisionFallback | None:
    """La decisión para las fuentes fallidas; None si no falló ninguna (recorrido nominal)."""
    if not dependencias:
        return None
    vigentes = reglas if reglas is not None and reglas.vigentes_en(ahora) else None
    por_fuente = {r.fuente: r for r in vigentes.reglas} if vigentes else {}
    faltantes: list[Faltante] = []
    ajustes: list[FactorAplicado] = []
    pendientes: list[ReglaFallback | None] = []
    ordenadas = sorted(dependencias, key=lambda d: d.fuente)
    for dependencia in ordenadas:
        regla = por_fuente.get(dependencia.fuente)
        if _degradable(dependencia, regla) and regla is not None:
            if regla.ajuste is not None:
                ajustes.append(FactorAplicado(FACTOR_FALLBACK, dependencia.fuente, regla.ajuste))
        else:
            pendientes.append(regla)
        aplicada = (
            None
            if regla is None or vigentes is None
            else ReglaAplicada(regla.id, vigentes.version, regla.accion)
        )
        faltantes.extend(
            Faltante(
                dependencia.fuente, dato, dependencia.motivo, dependencia.obligatoria, aplicada
            )
            for dato in dependencia.datos
        )
    # Sin reglas vigentes nada es degradable: `pendientes` no está vacía.
    if pendientes or vigentes is None:
        return DecisionFallback(
            tipo=TipoDecision.PENDIENTE,
            faltantes=tuple(faltantes),
            mensaje_codigo=_mensaje(pendientes, MensajeFallback.FUENTE_REQUERIDA_NO_DISPONIBLE),
            version_reglas=vigentes.version if vigentes else None,
            hash_reglas=vigentes.hash if vigentes else None,
        )
    return DecisionFallback(
        tipo=TipoDecision.DEGRADADA,
        faltantes=tuple(faltantes),
        mensaje_codigo=_mensaje(
            (por_fuente[d.fuente] for d in ordenadas),
            MensajeFallback.FUENTE_OPCIONAL_NO_DISPONIBLE,
        ),
        version_reglas=vigentes.version,
        hash_reglas=vigentes.hash,
        ajustes=tuple(ajustes),
    )


def _degradable(dependencia: FuenteFallida, regla: ReglaFallback | None) -> bool:
    """Solo con una regla DEGRADAR, y nunca una obligatoria (lo diga el perfil o la regla)."""
    return (
        regla is not None
        and regla.accion is AccionFallback.DEGRADAR
        and not regla.obligatoria
        and not dependencia.obligatoria
    )


def _mensaje(
    reglas: Iterable[ReglaFallback | None], por_omision: MensajeFallback
) -> MensajeFallback:
    """El mensaje de la primera regla que decidió (orden por fuente); sin regla, el de omisión."""
    return next((r.mensaje_codigo for r in reglas if r is not None), por_omision)
