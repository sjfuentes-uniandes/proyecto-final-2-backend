"""UT-W14-01: comunicación de la decisión por tabla (HU-W14, W14-B01).

Tres resultados × regla explicable/reservada × oferta vigente/vencida, la regla R-12 del motivo en
revisión y el rechazo sin alternativas. Las decisiones salen del motor con la semilla 2026.10.1 y
los casos sintéticos de contracts/datos-sinteticos/cotizaciones-suscripcion.json.
"""

import dataclasses
import re
from collections.abc import Iterator
from datetime import datetime, timedelta
from itertools import product
from typing import Any
from uuid import UUID

import pytest
from polizas.domain.suscripcion.comunicacion import (
    MOTIVO_NO_ELEGIBLE,
    TABLA,
    comunicar,
    motivo_rechazo,
    motivo_revision,
)
from polizas.domain.suscripcion.modelos import (
    Alternativa,
    Comunicacion,
    CondicionAseguramiento,
    Decision,
    DescriptorAlternativa,
    InsumosSuscripcion,
    Regla,
    ReglasSuscripcion,
    ResultadoDecision,
    SiguientePaso,
    TipoDecision,
)
from polizas.domain.suscripcion.motor import evaluar

COMPROMISO_HORAS = 8
CODIGO_REGLA = re.compile(r"SUS-\d{3}")


def _decision(insumos: InsumosSuscripcion, reglas: ReglasSuscripcion) -> Decision:
    return Decision(
        id=UUID(int=1),
        codigo="DEC-2026-000001",
        cotizacion_id=insumos.cotizacion_id,
        oferta_id=insumos.oferta_id,
        cotizacion_codigo=insumos.codigo,
        cliente_id=insumos.cliente_id,
        producto_id=insumos.producto_id,
        tipo=TipoDecision.AUTOMATICA,
        evaluacion=evaluar(insumos, reglas.reglas),
        version_reglas=reglas.version,
        hash_reglas=reglas.hash,
        insumos=insumos,
        hash_insumos="huella-insumos",
        decidida_en=insumos.calculado_en,
        duracion_ms=480,
        idempotency_key=UUID(int=2),
        huella_solicitud="huella-solicitud",
        correlation_id="correlacion-prueba",
        actor_id="sub-asesor-1",
    )


def _alternativas(sinteticos: Any) -> tuple[Alternativa, ...]:
    """Las alternativas de la semilla 2026.10.1, en su orden."""
    return tuple(
        Alternativa(a["productoId"], DescriptorAlternativa(**a["descriptor"]))
        for a in sinteticos.respuesta_reglas()["definicion"]["alternativas"]
    )


def _con_regla(reglas: ReglasSuscripcion, codigo: str, **cambios: Any) -> ReglasSuscripcion:
    return dataclasses.replace(
        reglas,
        reglas=tuple(
            dataclasses.replace(r, **cambios) if r.codigo == codigo else r for r in reglas.reglas
        ),
    )


def _textos(valor: Any) -> Iterator[str]:
    """Todos los textos de la comunicación, recorriendo sus dataclasses y tuplas."""
    if dataclasses.is_dataclass(valor) and not isinstance(valor, type):
        for campo in dataclasses.fields(valor):
            yield from _textos(getattr(valor, campo.name))
    elif isinstance(valor, tuple):
        for elemento in valor:
            yield from _textos(elemento)
    elif isinstance(valor, str):
        yield valor


def _comunicar(sinteticos: Any, caso_id: str, ahora: datetime | None = None) -> Comunicacion:
    decision = _decision(sinteticos.insumos(caso_id), sinteticos.reglas())
    return comunicar(
        decision, _alternativas(sinteticos), ahora or sinteticos.ahora, COMPROMISO_HORAS
    )


def test_la_tabla_cubre_cada_resultado_con_oferta_vigente_y_vencida() -> None:
    assert set(TABLA) == set(product(ResultadoDecision, (True, False)))


# Caso sintético → (condición, siguiente paso con la oferta vigente, motivo, ¿alternativas?,
# ¿plazo?). Con la oferta vencida solo cambia la aprobada (COTIZAR_NUEVAMENTE, sin contratación).
ESPERADO: dict[str, tuple[CondicionAseguramiento, SiguientePaso, str | None, bool, bool]] = {
    "S01": (
        CondicionAseguramiento.APROBADA_SIN_AJUSTES,
        SiguientePaso.ACEPTAR_OFERTA,
        None,
        False,
        False,
    ),
    "S02": (
        CondicionAseguramiento.NO_ASEGURABLE,
        SiguientePaso.COTIZAR_ALTERNATIVA,
        "EDAD_FUERA_DE_RANGO",
        True,
        False,
    ),
    "S03": (
        CondicionAseguramiento.NO_ASEGURABLE,
        SiguientePaso.COTIZAR_ALTERNATIVA,
        "PLAZO_FUERA_DE_RANGO",
        True,
        False,
    ),
    "S04": (
        CondicionAseguramiento.NO_ASEGURABLE,
        SiguientePaso.COTIZAR_ALTERNATIVA,
        MOTIVO_NO_ELEGIBLE,
        True,
        False,
    ),
    "S05": (
        CondicionAseguramiento.EN_REVISION,
        SiguientePaso.ESPERAR_REVISION,
        "SUMA_REQUIERE_REVISION",
        False,
        True,
    ),
    "S06": (
        CondicionAseguramiento.EN_REVISION,
        SiguientePaso.ESPERAR_REVISION,
        "SUMA_REQUIERE_REVISION",
        False,
        True,
    ),
    "S07": (
        CondicionAseguramiento.EN_REVISION,
        SiguientePaso.ESPERAR_REVISION,
        "PERFIL_REQUIERE_REVISION",
        False,
        True,
    ),
    "S08": (
        CondicionAseguramiento.EN_REVISION,
        SiguientePaso.ESPERAR_REVISION,
        "PERFIL_REQUIERE_REVISION",
        False,
        True,
    ),
}


@pytest.mark.parametrize(("caso_id", "vigente"), list(product(ESPERADO, (True, False))))
def test_comunicacion_por_tabla(sinteticos: Any, caso_id: str, vigente: bool) -> None:
    insumos = sinteticos.insumos(caso_id)
    ahora = sinteticos.ahora if vigente else insumos.vigente_hasta + timedelta(days=1)
    condicion, paso, motivo, con_alternativas, con_plazo = ESPERADO[caso_id]
    aprobada = condicion is CondicionAseguramiento.APROBADA_SIN_AJUSTES
    if aprobada and not vigente:
        paso = SiguientePaso.COTIZAR_NUEVAMENTE

    comunicacion = _comunicar(sinteticos, caso_id, ahora)

    assert comunicacion.condicion is condicion
    assert comunicacion.siguiente_paso is paso
    assert comunicacion.admite_contratacion is (aprobada and vigente)
    assert comunicacion.motivo_cliente == motivo
    assert comunicacion.alternativas == (_alternativas(sinteticos) if con_alternativas else ())
    assert comunicacion.plazo_respuesta_horas == (COMPROMISO_HORAS if con_plazo else None)
    assert comunicacion.vigente_hasta == (insumos.vigente_hasta if aprobada else None)


def test_aprobada_vigente_lleva_la_vigencia_de_las_condiciones(sinteticos: Any) -> None:
    """AC1: condición, vigencia y opción de aceptar."""
    comunicacion = _comunicar(sinteticos, "S01")
    assert comunicacion == Comunicacion(
        condicion=CondicionAseguramiento.APROBADA_SIN_AJUSTES,
        admite_contratacion=True,
        siguiente_paso=SiguientePaso.ACEPTAR_OFERTA,
        vigente_hasta=datetime.fromisoformat("2026-10-26T14:00:00Z"),
        motivo_cliente=None,
        alternativas=(),
        plazo_respuesta_horas=None,
    )


@pytest.mark.parametrize(
    ("desplazamiento", "admite", "paso"),
    [
        (timedelta(microseconds=-1), True, SiguientePaso.ACEPTAR_OFERTA),
        (timedelta(0), False, SiguientePaso.COTIZAR_NUEVAMENTE),
        (timedelta(microseconds=1), False, SiguientePaso.COTIZAR_NUEVAMENTE),
    ],
)
def test_aprobada_en_los_limites_de_la_vigencia(
    sinteticos: Any, desplazamiento: timedelta, admite: bool, paso: SiguientePaso
) -> None:
    """El instante exacto vigenteHasta ya es vencido (DS2-10): sin opción de aceptar."""
    vigente_hasta = sinteticos.insumos("S01").vigente_hasta
    comunicacion = _comunicar(sinteticos, "S01", vigente_hasta + desplazamiento)
    assert comunicacion.admite_contratacion is admite
    assert comunicacion.siguiente_paso is paso
    assert comunicacion.condicion is CondicionAseguramiento.APROBADA_SIN_AJUSTES
    assert comunicacion.vigente_hasta == vigente_hasta


def test_rechazo_por_regla_reservada_es_no_elegible_aunque_declare_otro_motivo(
    sinteticos: Any,
) -> None:
    """DS2-07: el motivo de una RESERVADA hacia el cliente siempre es el genérico."""
    reglas = _con_regla(sinteticos.reglas(), "SUS-015", motivo_cliente="EDAD_FUERA_DE_RANGO")
    decision = _decision(sinteticos.insumos("S04"), reglas)
    assert decision.evaluacion.regla_determinante == "SUS-015"
    comunicacion = comunicar(decision, (), sinteticos.ahora, COMPROMISO_HORAS)
    assert comunicacion.motivo_cliente == MOTIVO_NO_ELEGIBLE


def test_rechazo_sin_regla_determinante_conocida_es_no_elegible(sinteticos: Any) -> None:
    """Defensa: si la determinante no está entre las evaluadas, nunca se inventa un motivo."""
    decision = _decision(sinteticos.insumos("S02"), sinteticos.reglas())
    evaluacion = dataclasses.replace(decision.evaluacion, regla_determinante="SUS-999")
    assert motivo_rechazo(evaluacion) == MOTIVO_NO_ELEGIBLE


@pytest.mark.parametrize("caso_id", list(ESPERADO))
def test_la_comunicacion_nunca_lleva_codigos_ni_valores_de_reglas(
    sinteticos: Any, caso_id: str
) -> None:
    decision = _decision(sinteticos.insumos(caso_id), sinteticos.reglas())
    comunicacion = comunicar(
        decision, _alternativas(sinteticos), sinteticos.ahora, COMPROMISO_HORAS
    )
    textos = list(_textos(comunicacion))
    assert not [t for t in textos if CODIGO_REGLA.search(t)]
    reservadas = [r for r in decision.evaluacion.reglas if r.regla.visibilidad.value == "RESERVADA"]
    valores_reservados = {r.valor_caso for r in reservadas if r.valor_caso is not None}
    assert not valores_reservados & set(textos)


def test_rechazo_sin_alternativas(sinteticos: Any) -> None:
    decision = _decision(sinteticos.insumos("S02"), sinteticos.reglas())
    comunicacion = comunicar(decision, [], sinteticos.ahora, COMPROMISO_HORAS)
    assert comunicacion.alternativas == ()
    assert comunicacion.siguiente_paso is SiguientePaso.COTIZAR_ALTERNATIVA
    assert comunicacion.admite_contratacion is False


@pytest.mark.parametrize("caso_id", ["S01", "S06"])
def test_las_alternativas_solo_se_comunican_en_un_rechazo(sinteticos: Any, caso_id: str) -> None:
    assert _comunicar(sinteticos, caso_id).alternativas == ()


def test_revision_no_promete_cobertura(sinteticos: Any) -> None:
    """AC3: siguiente paso y plazo, sin vigencia, alternativas ni opción de contratar."""
    comunicacion = _comunicar(sinteticos, "S06")
    assert comunicacion == Comunicacion(
        condicion=CondicionAseguramiento.EN_REVISION,
        admite_contratacion=False,
        siguiente_paso=SiguientePaso.ESPERAR_REVISION,
        vigente_hasta=None,
        motivo_cliente="SUMA_REQUIERE_REVISION",
        alternativas=(),
        plazo_respuesta_horas=COMPROMISO_HORAS,
    )


def test_revision_solo_por_regla_reservada_no_tiene_motivo(sinteticos: Any) -> None:
    """R-12: listas no consultadas (SUS-015 NO_EVALUABLE) → revisión sin motivo, nunca
    NO_ELEGIBLE."""
    insumos = dataclasses.replace(sinteticos.insumos("S01"), coincidencias_listas_restrictivas=None)
    decision = _decision(insumos, sinteticos.reglas())
    assert decision.resultado is ResultadoDecision.REVISION_REQUERIDA
    comunicacion = comunicar(decision, (), sinteticos.ahora, COMPROMISO_HORAS)
    assert comunicacion.condicion is CondicionAseguramiento.EN_REVISION
    assert comunicacion.motivo_cliente is None


def test_revision_toma_la_primera_regla_explicable_por_orden(sinteticos: Any) -> None:
    """R-12: S05 incumple SUS-004 (suma) y SUS-011 (confianza); manda el orden, no la posición."""
    decision = _decision(sinteticos.insumos("S05"), sinteticos.reglas())
    al_reves = dataclasses.replace(
        decision.evaluacion, reglas=tuple(reversed(decision.evaluacion.reglas))
    )
    assert motivo_revision(al_reves) == "SUMA_REQUIERE_REVISION"


def test_revision_salta_una_regla_explicable_con_motivo_no_elegible(sinteticos: Any) -> None:
    """Una revisión nunca comunica NO_ELEGIBLE, aunque una regla EXPLICABLE lo declare."""
    reglas = _con_regla(sinteticos.reglas(), "SUS-007", motivo_cliente=MOTIVO_NO_ELEGIBLE)
    decision = _decision(sinteticos.insumos("S08"), reglas)
    assert decision.resultado is ResultadoDecision.REVISION_REQUERIDA
    assert motivo_revision(decision.evaluacion) == "PERFIL_REQUIERE_REVISION"


def test_revision_con_regla_excluyente_no_evaluable_usa_su_motivo(sinteticos: Any) -> None:
    """Una EXCLUYENTE EXPLICABLE sin dato enruta a revisión (DS2-06) y aporta su motivo."""
    reglas: ReglasSuscripcion = sinteticos.reglas()
    sin_dato: tuple[Regla, ...] = tuple(
        dataclasses.replace(r, campo="campo_inexistente") if r.codigo == "SUS-001" else r
        for r in reglas.reglas
    )
    decision = _decision(sinteticos.insumos("S01"), dataclasses.replace(reglas, reglas=sin_dato))
    assert decision.resultado is ResultadoDecision.REVISION_REQUERIDA
    assert motivo_revision(decision.evaluacion) == "EDAD_FUERA_DE_RANGO"


def test_es_pura_y_determinista(sinteticos: Any) -> None:
    decision = _decision(sinteticos.insumos("S02"), sinteticos.reglas())
    alternativas = list(_alternativas(sinteticos))
    primera = comunicar(decision, alternativas, sinteticos.ahora, COMPROMISO_HORAS)
    segunda = comunicar(decision, alternativas, sinteticos.ahora, COMPROMISO_HORAS)
    assert primera == segunda
    assert alternativas == list(_alternativas(sinteticos))
    with pytest.raises(dataclasses.FrozenInstanceError):
        primera.admite_contratacion = True  # type: ignore[misc]
