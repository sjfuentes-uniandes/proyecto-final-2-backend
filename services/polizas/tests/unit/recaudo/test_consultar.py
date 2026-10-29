"""Estado del recaudo (dominio) y ConsultarRecaudo con los dobles (HU-W16, 16.13)."""

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from polizas.application.aceptacion.registrar import (
    ComandoAceptacion,
    ConfigAceptacion,
    RegistrarAceptacion,
)
from polizas.application.recaudo.consultar import ConsultarRecaudo
from polizas.application.suscripcion.solicitar_suscripcion import (
    ComandoSuscripcion,
    ConfigSuscripcion,
    SolicitarSuscripcion,
)
from polizas.domain.aceptacion.errores import AceptacionNoEncontrada
from polizas.domain.aceptacion.modelos import Aceptacion, Declaraciones
from polizas.domain.recaudo.modelos import (
    EstadoRecaudo,
    ResultadoRecaudo,
    SituacionRecaudo,
    estado_recaudo,
)
from polizas.domain.suscripcion.errores import DependenciaNoDisponible
from polizas.domain.suscripcion.modelos import Actor, Decision, RolActor

pytestmark = pytest.mark.anyio
ASESOR = Actor("sub-asesor-1", RolActor.ASESOR)
RESUELTO_EN = datetime(2026, 10, 24, 15, 0, 1, tzinfo=UTC)


async def _aceptada(dobles: Any, sinteticos: Any) -> tuple[Aceptacion, Decision]:
    decision, _ = await SolicitarSuscripcion(
        dobles.insumos,
        dobles.reglas,
        dobles.decisiones,
        dobles.codigos,
        dobles.reloj,
        ConfigSuscripcion(),
    ).ejecutar(ComandoSuscripcion(UUID(sinteticos.cotizacion_id("S01")), uuid4(), ASESOR, "c"))
    # Se acepta 3 minutos después de decidir: aceptada_en y decidida_en se distinguen.
    dobles.reloj.instante += timedelta(minutes=3)
    aceptacion, _ = await RegistrarAceptacion(
        dobles.decisiones, dobles.aceptaciones, dobles.reloj, ConfigAceptacion()
    ).ejecutar(
        ComandoAceptacion(
            decision.cotizacion_id,
            decision.id,
            "2026.10.1",
            Declaraciones(True, True, False, True),
            uuid4(),
            ASESOR,
            "corr",
        )
    )
    return aceptacion, decision


def _confirmado(clave: UUID) -> ResultadoRecaudo:
    return ResultadoRecaudo(
        clave_recaudo=clave,
        recaudo_id=uuid4(),
        estado=SituacionRecaudo.CONFIRMADO,
        monto=Decimal("64638.00"),
        moneda="COP",
        transaccion_id="TX-1",
        motivo=None,
        ocurrido_en=RESUELTO_EN,
    )


def _rechazado(clave: UUID) -> ResultadoRecaudo:
    return ResultadoRecaudo(
        clave_recaudo=clave,
        recaudo_id=uuid4(),
        estado=SituacionRecaudo.RECHAZADO,
        monto=None,
        moneda=None,
        transaccion_id=None,
        motivo="FONDOS_INSUFICIENTES",
        ocurrido_en=RESUELTO_EN,
    )


# --- Dominio ---------------------------------------------------------------------------------
async def test_sin_resultado_pendiente_con_la_instantanea(dobles: Any, sinteticos: Any) -> None:
    aceptacion, decision = await _aceptada(dobles, sinteticos)
    assert aceptacion.aceptada_en != decision.decidida_en
    assert estado_recaudo(aceptacion, decision, None) == EstadoRecaudo(
        clave_recaudo=aceptacion.id,
        estado=SituacionRecaudo.PENDIENTE,
        monto=decision.insumos.prima_total,
        moneda=decision.insumos.moneda,
        transaccion_id=None,
        motivo=None,
        actualizado_en=aceptacion.aceptada_en,
    )


async def test_confirmado_con_el_monto_del_evento(dobles: Any, sinteticos: Any) -> None:
    aceptacion, decision = await _aceptada(dobles, sinteticos)
    # Un monto distinto del de la instantánea: manda el del evento (lo cobrado).
    resultado = dataclasses.replace(_confirmado(aceptacion.id), monto=Decimal("1.50"), moneda="USD")
    assert estado_recaudo(aceptacion, decision, resultado) == EstadoRecaudo(
        clave_recaudo=aceptacion.id,
        estado=SituacionRecaudo.CONFIRMADO,
        monto=Decimal("1.50"),
        moneda="USD",
        transaccion_id="TX-1",
        motivo=None,
        actualizado_en=RESUELTO_EN,
    )


async def test_rechazado_sin_monto_toma_el_de_la_instantanea(dobles: Any, sinteticos: Any) -> None:
    aceptacion, decision = await _aceptada(dobles, sinteticos)
    estado = estado_recaudo(aceptacion, decision, _rechazado(aceptacion.id))
    assert (estado.estado, estado.motivo, estado.transaccion_id) == (
        SituacionRecaudo.RECHAZADO,
        "FONDOS_INSUFICIENTES",
        None,
    )
    assert (estado.monto, estado.moneda) == (
        decision.insumos.prima_total,
        decision.insumos.moneda,
    )
    assert estado.actualizado_en == RESUELTO_EN


# --- Caso de uso -----------------------------------------------------------------------------
def _caso(dobles: Any) -> ConsultarRecaudo:
    return ConsultarRecaudo(dobles.aceptaciones, dobles.decisiones, dobles.recaudos)


async def test_sin_aceptacion_lanza_no_encontrada(dobles: Any, telemetria: Any) -> None:
    with pytest.raises(AceptacionNoEncontrada):
        await _caso(dobles).por_cotizacion(uuid4())
    assert telemetria.resultados("recaudo.consultar") == ["rechazado"]


async def test_lee_el_resultado_por_la_clave_de_la_aceptacion(
    dobles: Any, sinteticos: Any, telemetria: Any
) -> None:
    aceptacion, _ = await _aceptada(dobles, sinteticos)
    dobles.recaudos.resultados[aceptacion.id] = _confirmado(aceptacion.id)
    estado = await _caso(dobles).por_cotizacion(aceptacion.cotizacion_id)
    assert (estado.estado, estado.clave_recaudo) == (SituacionRecaudo.CONFIRMADO, aceptacion.id)
    assert telemetria.resultados("recaudo.consultar") == ["ok"]


async def test_sin_la_decision_es_una_dependencia_no_disponible(
    dobles: Any, sinteticos: Any
) -> None:
    # No pasa: la aceptación tiene llave hacia su decisión y las dos son inmutables.
    aceptacion, _ = await _aceptada(dobles, sinteticos)
    dobles.decisiones.decisiones.clear()
    with pytest.raises(DependenciaNoDisponible):
        await _caso(dobles).por_cotizacion(aceptacion.cotizacion_id)
