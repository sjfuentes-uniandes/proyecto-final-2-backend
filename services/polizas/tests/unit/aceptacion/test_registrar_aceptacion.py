"""UT-W15-02: RegistrarAceptacion y ConsultarAceptacion con los dobles (HU-W15, W15-B03).

Idempotencia (repetición, conflicto, ya aceptada), decisión que no coincide, carrera perdida con
la misma clave o con otra, validación y la operación medida (`ok`, `rechazado`, `error`).
"""

import dataclasses
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from polizas.application.aceptacion.consultar import ConsultarAceptacion
from polizas.application.aceptacion.registrar import (
    ComandoAceptacion,
    ConfigAceptacion,
    RegistrarAceptacion,
    huella_solicitud,
)
from polizas.application.suscripcion.solicitar_suscripcion import (
    ComandoSuscripcion,
    ConfigSuscripcion,
    SolicitarSuscripcion,
)
from polizas.domain.aceptacion.errores import (
    AceptacionNoEncontrada,
    DecisionNoAprobada,
    DeclaracionesIncompletas,
    OfertaVencida,
    OfertaYaAceptada,
    TextoNoVigente,
)
from polizas.domain.aceptacion.evidencia import huella_evidencia
from polizas.domain.aceptacion.modelos import CanalAceptacion, Declaraciones
from polizas.domain.suscripcion.errores import ConflictoIdempotencia, DecisionNoEncontrada
from polizas.domain.suscripcion.modelos import Actor, Decision, RolActor

pytestmark = pytest.mark.anyio

ASESOR = Actor("sub-asesor-1", RolActor.ASESOR)
TODAS = Declaraciones(True, True, True, True)
VERSION = "2026.10.1"
OPERACION = "aceptacion.registrar"


async def _decidir(dobles: Any, sinteticos: Any, id_caso: str) -> Decision:
    caso_uso = SolicitarSuscripcion(
        dobles.insumos,
        dobles.reglas,
        dobles.decisiones,
        dobles.codigos,
        dobles.reloj,
        ConfigSuscripcion(),
    )
    comando = ComandoSuscripcion(
        UUID(sinteticos.cotizacion_id(id_caso)), uuid4(), ASESOR, "corr-decision"
    )
    decision, _ = await caso_uso.ejecutar(comando)
    return decision


def _registrar(dobles: Any) -> RegistrarAceptacion:
    return RegistrarAceptacion(
        dobles.decisiones, dobles.aceptaciones, dobles.reloj, ConfigAceptacion(VERSION)
    )


def _comando(decision: Decision, clave: UUID | None = None, **cambios: Any) -> ComandoAceptacion:
    comando = ComandoAceptacion(
        cotizacion_id=decision.cotizacion_id,
        decision_id=decision.id,
        version_declaraciones=VERSION,
        declaraciones=TODAS,
        idempotency_key=clave or uuid4(),
        actor=ASESOR,
        correlation_id="corr-aceptacion",
    )
    return dataclasses.replace(comando, **cambios)


async def test_registra_la_aceptacion_con_su_evidencia_y_evento(
    dobles: Any, sinteticos: Any, telemetria: Any
) -> None:
    decision = await _decidir(dobles, sinteticos, "S01")
    comando = _comando(decision)
    aceptacion, creada = await _registrar(dobles).ejecutar(comando)
    assert creada is True
    assert (aceptacion.cotizacion_id, aceptacion.decision_id) == (
        decision.cotizacion_id,
        decision.id,
    )
    assert (aceptacion.oferta_id, aceptacion.cliente_id) == (
        decision.oferta_id,
        decision.cliente_id,
    )
    assert aceptacion.huella_calculo == decision.insumos.huella_calculo
    assert aceptacion.vigente_hasta == decision.insumos.vigente_hasta
    assert aceptacion.canal is CanalAceptacion.PORTAL_ASESOR
    assert (aceptacion.actor_id, aceptacion.aceptada_en) == ("sub-asesor-1", sinteticos.ahora)
    assert (aceptacion.correlation_id, aceptacion.idempotency_key) == (
        "corr-aceptacion",
        comando.idempotency_key,
    )
    assert aceptacion.huella_solicitud == huella_solicitud(comando)
    assert aceptacion.huella_evidencia == huella_evidencia(aceptacion)
    assert dobles.aceptaciones.eventos == [(aceptacion, decision)]
    assert telemetria.resultados(OPERACION) == ["ok"]
    assert "aceptacion registrada" in telemetria.texto_logs()


async def test_la_misma_clave_devuelve_la_existente_sin_otro_evento(
    dobles: Any, sinteticos: Any, telemetria: Any
) -> None:
    decision = await _decidir(dobles, sinteticos, "S01")
    comando = _comando(decision)
    primera, _ = await _registrar(dobles).ejecutar(comando)
    # Aunque después venza la oferta, la repetición devuelve lo registrado (AC3).
    dobles.reloj.instante = decision.insumos.vigente_hasta + timedelta(days=1)
    repetida, creada = await _registrar(dobles).ejecutar(comando)
    assert (repetida, creada) == (primera, False)
    assert len(dobles.aceptaciones.eventos) == 1
    assert telemetria.resultados(OPERACION) == ["ok", "ok"]


@pytest.mark.parametrize(
    "cambio",
    [
        {"declaraciones": Declaraciones(True, True, False, True)},
        {"version_declaraciones": "2026.09.1"},
        {"decision_id": UUID(int=9)},
        {"cotizacion_id": UUID(int=9)},
    ],
    ids=lambda cambio: next(iter(cambio)),
)
async def test_la_misma_clave_con_otro_cuerpo_es_conflicto(
    dobles: Any, sinteticos: Any, cambio: dict[str, Any]
) -> None:
    decision = await _decidir(dobles, sinteticos, "S01")
    comando = _comando(decision)
    await _registrar(dobles).ejecutar(comando)
    with pytest.raises(ConflictoIdempotencia):
        await _registrar(dobles).ejecutar(dataclasses.replace(comando, **cambio))


async def test_otra_clave_sobre_una_oferta_aceptada(
    dobles: Any, sinteticos: Any, telemetria: Any
) -> None:
    decision = await _decidir(dobles, sinteticos, "S01")
    primera, _ = await _registrar(dobles).ejecutar(_comando(decision))
    # Antes de validar: aunque la nueva solicitud falle otras condiciones, ya está aceptada.
    otra = _comando(decision, declaraciones=Declaraciones(False, False, False, False))
    with pytest.raises(OfertaYaAceptada) as error:
        await _registrar(dobles).ejecutar(otra)
    assert error.value.aceptacion_id == primera.id
    assert len(dobles.aceptaciones.eventos) == 1
    assert telemetria.resultados(OPERACION) == ["ok", "rechazado"]


async def test_sin_decision_o_con_otra_decision_no_se_encuentra(
    dobles: Any, sinteticos: Any
) -> None:
    decision = await _decidir(dobles, sinteticos, "S01")
    with pytest.raises(DecisionNoEncontrada):
        await _registrar(dobles).ejecutar(_comando(decision, decision_id=uuid4()))
    sin_decidir = dataclasses.replace(decision, cotizacion_id=UUID(sinteticos.cotizacion_id("S06")))
    with pytest.raises(DecisionNoEncontrada):
        await _registrar(dobles).ejecutar(_comando(sin_decidir))
    assert dobles.aceptaciones.aceptaciones == []


@pytest.mark.parametrize(
    ("caso", "cambio", "error"),
    [
        ("S02", {}, DecisionNoAprobada),
        ("S06", {}, DecisionNoAprobada),
        ("S01", {"version_declaraciones": "2026.09.1"}, TextoNoVigente),
        (
            "S01",
            {"declaraciones": Declaraciones(True, False, True, True)},
            DeclaracionesIncompletas,
        ),
    ],
)
async def test_la_validacion_rechaza_sin_registrar(
    dobles: Any,
    sinteticos: Any,
    telemetria: Any,
    caso: str,
    cambio: dict[str, Any],
    error: type[Exception],
) -> None:
    decision = await _decidir(dobles, sinteticos, caso)
    with pytest.raises(error):
        await _registrar(dobles).ejecutar(_comando(decision, **cambio))
    assert dobles.aceptaciones.aceptaciones == []
    assert telemetria.resultados(OPERACION) == ["rechazado"]


@pytest.mark.parametrize("despues", [timedelta(0), timedelta(days=8)], ids=str)
async def test_la_oferta_vencida_no_se_acepta(
    dobles: Any, sinteticos: Any, despues: timedelta
) -> None:
    decision = await _decidir(dobles, sinteticos, "S01")
    dobles.reloj.instante = decision.insumos.vigente_hasta + despues
    with pytest.raises(OfertaVencida):
        await _registrar(dobles).ejecutar(_comando(decision))
    assert dobles.aceptaciones.eventos == []


async def test_carrera_perdida_con_la_misma_clave_devuelve_la_ganadora(
    dobles: Any, sinteticos: Any
) -> None:
    decision = await _decidir(dobles, sinteticos, "S01")
    comando = _comando(decision)
    ganadora, _ = await _registrar(dobles).ejecutar(comando)
    dobles.aceptaciones.aceptaciones.clear()
    dobles.aceptaciones.ganadora = ganadora
    # Las lecturas previas no la ven (aún no existía); guardar pierde y la relee por la clave.
    resultado, creada = await _registrar(dobles).ejecutar(comando)
    assert (resultado, creada) == (ganadora, False)


async def test_carrera_perdida_con_otra_clave_es_oferta_ya_aceptada(
    dobles: Any, sinteticos: Any
) -> None:
    decision = await _decidir(dobles, sinteticos, "S01")
    ganadora, _ = await _registrar(dobles).ejecutar(_comando(decision))
    dobles.aceptaciones.aceptaciones.clear()
    dobles.aceptaciones.ganadora = ganadora
    with pytest.raises(OfertaYaAceptada) as error:
        await _registrar(dobles).ejecutar(_comando(decision))
    assert error.value.aceptacion_id == ganadora.id


async def test_carrera_perdida_sin_ganadora_visible_es_conflicto(
    dobles: Any, sinteticos: Any
) -> None:
    decision = await _decidir(dobles, sinteticos, "S01")
    ajena, _ = await _registrar(dobles).ejecutar(_comando(decision))
    dobles.aceptaciones.aceptaciones.clear()
    # La clave la ganó una aceptación de otra cotización.
    dobles.aceptaciones.ganadora = dataclasses.replace(ajena, cotizacion_id=uuid4())
    with pytest.raises(ConflictoIdempotencia):
        await _registrar(dobles).ejecutar(_comando(decision))


async def test_una_falla_tecnica_se_mide_como_error(
    dobles: Any, sinteticos: Any, telemetria: Any
) -> None:
    decision = await _decidir(dobles, sinteticos, "S01")
    dobles.aceptaciones.falla = OSError("base caída")
    with pytest.raises(OSError):
        await _registrar(dobles).ejecutar(_comando(decision))
    assert telemetria.resultados(OPERACION) == ["error"]


async def test_consultar(dobles: Any, sinteticos: Any, telemetria: Any) -> None:
    decision = await _decidir(dobles, sinteticos, "S01")
    consultar = ConsultarAceptacion(dobles.aceptaciones)
    with pytest.raises(AceptacionNoEncontrada):
        await consultar.por_cotizacion(decision.cotizacion_id)
    aceptacion, _ = await _registrar(dobles).ejecutar(_comando(decision))
    assert await consultar.por_cotizacion(decision.cotizacion_id) == aceptacion
    assert telemetria.resultados("aceptacion.consultar") == ["rechazado", "ok"]
