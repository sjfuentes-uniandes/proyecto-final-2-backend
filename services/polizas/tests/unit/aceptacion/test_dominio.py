"""UT-W15-01: validación y evidencia de la aceptación (HU-W15, W15-B02).

Precedencia de la validación (texto → decisión → vigencia → declaraciones), vigencia antes del
límite, en el instante exacto y después, declaraciones obligatorias y opcionales, versión del texto
y huella de la evidencia. Las decisiones salen del motor con la semilla 2026.10.1 y los casos
sintéticos de contracts/datos-sinteticos/cotizaciones-suscripcion.json.
"""

import dataclasses
import re
from datetime import UTC, datetime, timedelta, timezone
from itertools import product
from typing import Any
from uuid import UUID

import pytest
from polizas.domain.aceptacion.errores import (
    DecisionNoAprobada,
    DeclaracionesIncompletas,
    OfertaVencida,
    OfertaYaAceptada,
    TextoNoVigente,
)
from polizas.domain.aceptacion.evidencia import evidencia_canonica, huella_evidencia
from polizas.domain.aceptacion.modelos import Aceptacion, CanalAceptacion, Declaraciones
from polizas.domain.aceptacion.validacion import declaraciones_faltantes, validar_aceptacion
from polizas.domain.suscripcion.errores import OfertaVencida as OfertaVencidaSuscripcion
from polizas.domain.suscripcion.modelos import Decision, ResultadoDecision, TipoDecision
from polizas.domain.suscripcion.motor import evaluar

VERSION = "2026.10.1"
TODAS = Declaraciones(True, True, True, True)
OBLIGATORIAS = Declaraciones(True, True, False, False)
NINGUNA = Declaraciones(False, False, False, False)
HUELLA = re.compile(r"^sha256:[0-9a-f]{64}$")


def _decision(sinteticos: Any, caso_id: str) -> Decision:
    insumos = sinteticos.insumos(caso_id)
    reglas = sinteticos.reglas()
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


def _validar(
    decision: Decision,
    ahora: datetime,
    declaraciones: Declaraciones = TODAS,
    version: str = VERSION,
) -> None:
    validar_aceptacion(decision, declaraciones, version, VERSION, ahora)


def _aceptacion(decision: Decision, **cambios: Any) -> Aceptacion:
    base = Aceptacion(
        id=UUID(int=10),
        cotizacion_id=decision.cotizacion_id,
        oferta_id=decision.oferta_id,
        decision_id=decision.id,
        cliente_id=decision.cliente_id,
        huella_calculo=decision.insumos.huella_calculo,
        vigente_hasta=decision.insumos.vigente_hasta,
        version_declaraciones=VERSION,
        declaraciones=TODAS,
        canal=CanalAceptacion.PORTAL_ASESOR,
        actor_id="sub-asesor-1",
        aceptada_en=datetime(2026, 10, 24, 15, 0, tzinfo=UTC),
        idempotency_key=UUID(int=11),
        huella_solicitud="huella-solicitud",
        huella_evidencia="",
        correlation_id="corr-1",
    )
    return dataclasses.replace(base, **cambios)


# --- Validación --------------------------------------------------------------------------------


def test_aprobada_vigente_con_todas_las_declaraciones_procede(sinteticos: Any) -> None:
    _validar(_decision(sinteticos, "S01"), sinteticos.ahora)


def test_las_declaraciones_opcionales_no_bloquean(sinteticos: Any) -> None:
    _validar(_decision(sinteticos, "S01"), sinteticos.ahora, OBLIGATORIAS)


@pytest.mark.parametrize("caso_id", ["S02", "S03", "S04", "S05", "S06", "S07", "S08"])
def test_una_decision_no_aprobada_no_se_acepta(sinteticos: Any, caso_id: str) -> None:
    decision = _decision(sinteticos, caso_id)
    assert decision.resultado is not ResultadoDecision.APROBADA
    with pytest.raises(DecisionNoAprobada):
        _validar(decision, sinteticos.ahora)


def test_vigente_un_instante_antes_del_limite(sinteticos: Any) -> None:
    decision = _decision(sinteticos, "S01")
    _validar(decision, decision.insumos.vigente_hasta - timedelta(microseconds=1))


@pytest.mark.parametrize(
    "despues", [timedelta(0), timedelta(microseconds=1), timedelta(days=8)], ids=str
)
def test_vencida_en_el_instante_exacto_y_despues(sinteticos: Any, despues: timedelta) -> None:
    decision = _decision(sinteticos, "S01")
    with pytest.raises(OfertaVencida):
        _validar(decision, decision.insumos.vigente_hasta + despues)


def test_la_oferta_vencida_es_la_misma_de_la_suscripcion() -> None:
    assert OfertaVencida is OfertaVencidaSuscripcion


def test_la_vigencia_compara_instantes_y_no_la_zona_horaria(sinteticos: Any) -> None:
    decision = _decision(sinteticos, "S01")
    bogota = timezone(timedelta(hours=-5))
    limite_en_bogota = decision.insumos.vigente_hasta.astimezone(bogota)
    with pytest.raises(OfertaVencida):
        _validar(decision, limite_en_bogota)


@pytest.mark.parametrize(
    ("leido", "veraz", "faltantes"),
    [
        (False, True, ("leidoCondiciones",)),
        (True, False, ("informacionVeraz",)),
        (False, False, ("leidoCondiciones", "informacionVeraz")),
    ],
)
def test_faltan_declaraciones_obligatorias(
    sinteticos: Any, leido: bool, veraz: bool, faltantes: tuple[str, ...]
) -> None:
    for debito, avisos in product((True, False), repeat=2):
        declaraciones = Declaraciones(leido, veraz, debito, avisos)
        with pytest.raises(DeclaracionesIncompletas) as error:
            _validar(_decision(sinteticos, "S01"), sinteticos.ahora, declaraciones)
        assert error.value.campos == faltantes
        assert declaraciones_faltantes(declaraciones) == faltantes


@pytest.mark.parametrize("version", ["2026.09.1", "2026.10.2", "", " 2026.10.1"])
def test_otra_version_del_texto_no_es_vigente(sinteticos: Any, version: str) -> None:
    with pytest.raises(TextoNoVigente):
        _validar(_decision(sinteticos, "S01"), sinteticos.ahora, version=version)


# Precedencia: con varias condiciones fallando a la vez, gana la primera de la tabla.
@pytest.mark.parametrize(
    ("caso_id", "vencida", "declaraciones", "version", "error"),
    [
        ("S02", True, NINGUNA, "2026.09.1", TextoNoVigente),
        ("S06", True, NINGUNA, "2026.09.1", TextoNoVigente),
        ("S01", True, NINGUNA, "2026.09.1", TextoNoVigente),
        ("S02", True, NINGUNA, VERSION, DecisionNoAprobada),
        ("S06", False, NINGUNA, VERSION, DecisionNoAprobada),
        ("S01", True, NINGUNA, VERSION, OfertaVencida),
        ("S01", False, NINGUNA, VERSION, DeclaracionesIncompletas),
    ],
)
def test_precedencia_de_la_validacion(
    sinteticos: Any,
    caso_id: str,
    vencida: bool,
    declaraciones: Declaraciones,
    version: str,
    error: type[Exception],
) -> None:
    decision = _decision(sinteticos, caso_id)
    ahora = decision.insumos.vigente_hasta if vencida else sinteticos.ahora
    with pytest.raises(error):
        _validar(decision, ahora, declaraciones, version)


# --- Huella de la evidencia --------------------------------------------------------------------


def test_la_huella_es_sha256_canonica_y_determinista(sinteticos: Any) -> None:
    aceptacion = _aceptacion(_decision(sinteticos, "S01"))
    assert HUELLA.match(huella_evidencia(aceptacion))
    assert huella_evidencia(aceptacion) == huella_evidencia(dataclasses.replace(aceptacion))


def test_la_evidencia_canonica_tiene_exactamente_lo_aceptado(sinteticos: Any) -> None:
    decision = _decision(sinteticos, "S01")
    canonica = evidencia_canonica(_aceptacion(decision))
    assert canonica == {
        "cotizacionId": str(decision.cotizacion_id),
        "ofertaId": str(decision.oferta_id),
        "decisionId": str(decision.id),
        "huellaCalculo": decision.insumos.huella_calculo,
        "vigenteHasta": "2026-10-26T14:00:00Z",
        "versionDeclaraciones": VERSION,
        "declaraciones": {
            "leidoCondiciones": True,
            "informacionVeraz": True,
            "debitoAutomatico": True,
            "avisosElectronicos": True,
        },
        "clienteId": str(decision.cliente_id),
        "actorId": "sub-asesor-1",
        "canal": "PORTAL_ASESOR",
        "aceptadaEn": "2026-10-24T15:00:00Z",
    }


def test_la_evidencia_admite_oferta_nula(sinteticos: Any) -> None:
    aceptacion = _aceptacion(_decision(sinteticos, "S01"), oferta_id=None)
    assert evidencia_canonica(aceptacion)["ofertaId"] is None
    assert HUELLA.match(huella_evidencia(aceptacion))


@pytest.mark.parametrize(
    "cambio",
    [
        {"cotizacion_id": UUID(int=99)},
        {"oferta_id": None},
        {"decision_id": UUID(int=99)},
        {"cliente_id": UUID(int=99)},
        {"huella_calculo": "sha256:otra"},
        {"vigente_hasta": datetime(2026, 10, 27, 14, 0, tzinfo=UTC)},
        {"version_declaraciones": "2026.11.1"},
        {"declaraciones": OBLIGATORIAS},
        {"declaraciones": Declaraciones(True, True, True, False)},
        {"actor_id": "sub-asesor-2"},
        {"aceptada_en": datetime(2026, 10, 24, 15, 0, 1, tzinfo=UTC)},
    ],
    ids=lambda cambio: next(iter(cambio)),
)
def test_la_huella_cambia_con_cada_dato_de_la_evidencia(
    sinteticos: Any, cambio: dict[str, Any]
) -> None:
    aceptacion = _aceptacion(_decision(sinteticos, "S01"))
    assert huella_evidencia(dataclasses.replace(aceptacion, **cambio)) != huella_evidencia(
        aceptacion
    )


@pytest.mark.parametrize(
    "cambio",
    [
        {"id": UUID(int=77)},
        {"idempotency_key": UUID(int=77)},
        {"huella_solicitud": "otra"},
        {"huella_evidencia": "sha256:cualquiera"},
        {"correlation_id": "corr-2"},
    ],
    ids=lambda cambio: next(iter(cambio)),
)
def test_la_huella_no_depende_de_datos_tecnicos(sinteticos: Any, cambio: dict[str, Any]) -> None:
    aceptacion = _aceptacion(_decision(sinteticos, "S01"))
    assert huella_evidencia(dataclasses.replace(aceptacion, **cambio)) == huella_evidencia(
        aceptacion
    )


def test_la_huella_no_depende_de_la_zona_horaria_del_instante(sinteticos: Any) -> None:
    aceptacion = _aceptacion(_decision(sinteticos, "S01"))
    bogota = timezone(timedelta(hours=-5))
    en_bogota = dataclasses.replace(
        aceptacion,
        aceptada_en=aceptacion.aceptada_en.astimezone(bogota),
        vigente_hasta=aceptacion.vigente_hasta.astimezone(bogota),
    )
    assert huella_evidencia(en_bogota) == huella_evidencia(aceptacion)


def test_errores_con_detalles() -> None:
    assert DeclaracionesIncompletas(("informacionVeraz",)).campos == ("informacionVeraz",)
    assert OfertaYaAceptada(UUID(int=5)).aceptacion_id == UUID(int=5)
