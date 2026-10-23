"""Lecturas de la suscripción (decisión, bandeja, resumen DS2-14) y reconstrucción (HU-W13)."""

import dataclasses
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from polizas.application.suscripcion.consultar import (
    ConsultarSuscripciones,
    antiguedad_minutos,
    codificar_cursor,
    decodificar_cursor,
)
from polizas.application.suscripcion.reconstruir import ReconstruirDecision
from polizas.application.suscripcion.solicitar_suscripcion import (
    ComandoSuscripcion,
    ConfigSuscripcion,
    SolicitarSuscripcion,
)
from polizas.domain.suscripcion.errores import (
    CursorInvalido,
    DecisionNoEncontrada,
    DependenciaNoDisponible,
    RangoInvalido,
    ReglasNoVigentes,
)
from polizas.domain.suscripcion.modelos import (
    Actor,
    Decision,
    FiltrosBandeja,
    ResultadoDecision,
    RolActor,
)

pytestmark = pytest.mark.anyio
ASESOR = Actor("sub-asesor-1", RolActor.ASESOR)


async def _decidir(dobles: Any, sinteticos: Any, id_caso: str, minutos_antes: int = 0) -> Decision:
    """Decide el caso con el reloj `minutos_antes` del ahora de referencia."""
    instante = sinteticos.ahora - timedelta(minutes=minutos_antes)
    dobles.reloj.instante = instante
    caso_uso = SolicitarSuscripcion(
        dobles.insumos,
        dobles.reglas,
        dobles.decisiones,
        dobles.codigos,
        dobles.reloj,
        ConfigSuscripcion(),
    )
    comando = ComandoSuscripcion(UUID(sinteticos.cotizacion_id(id_caso)), uuid4(), ASESOR, "c")
    decision, _ = await caso_uso.ejecutar(comando)
    dobles.reloj.instante = sinteticos.ahora
    return decision


def _consultar(dobles: Any, compromiso: int = 8) -> ConsultarSuscripciones:
    return ConsultarSuscripciones(dobles.decisiones, dobles.reloj, compromiso)


async def test_decision_y_por_cotizacion(dobles: Any, sinteticos: Any) -> None:
    decision = await _decidir(dobles, sinteticos, "S01")
    consultar = _consultar(dobles)
    assert await consultar.decision(decision.id) == decision
    assert await consultar.por_cotizacion(decision.cotizacion_id) == decision
    assert await consultar.por_cotizacion(uuid4()) is None
    with pytest.raises(DecisionNoEncontrada):
        await consultar.decision(uuid4())


async def test_bandeja_del_mas_antiguo_al_mas_reciente_con_cursor(
    dobles: Any, sinteticos: Any
) -> None:
    s05 = await _decidir(dobles, sinteticos, "S05", minutos_antes=134)  # 2 h 14 min
    s06 = await _decidir(dobles, sinteticos, "S06", minutos_antes=48)
    s07 = await _decidir(dobles, sinteticos, "S07", minutos_antes=545)  # 9 h 05 min
    await _decidir(dobles, sinteticos, "S01")  # aprobada: no entra
    consultar = _consultar(dobles)
    primera = await consultar.bandeja(FiltrosBandeja(), 2, None)
    assert [c.decision_id for c in primera.casos] == [s07.id, s05.id]
    assert [c.antiguedad_minutos for c in primera.casos] == [545, 134]
    assert primera.total == 3 and primera.siguiente_cursor is not None
    segunda = await consultar.bandeja(FiltrosBandeja(), 2, primera.siguiente_cursor)
    assert [c.decision_id for c in segunda.casos] == [s06.id]
    assert segunda.siguiente_cursor is None
    assert segunda.casos[0].reglas_no_cumplidas == ("SUS-004",)
    assert segunda.casos[0].motivos == ("SUMA_REQUIERE_REVISION",)


async def test_bandeja_con_filtros(dobles: Any, sinteticos: Any) -> None:
    s05 = await _decidir(dobles, sinteticos, "S05")
    await _decidir(dobles, sinteticos, "S06", minutos_antes=1)
    consultar = _consultar(dobles)
    por_motivo = await consultar.bandeja(
        FiltrosBandeja(motivo="PERFIL_REQUIERE_REVISION"), 20, None
    )
    assert [c.decision_id for c in por_motivo.casos] == [s05.id]
    otro_producto = await consultar.bandeja(FiltrosBandeja(producto_id="otro"), 20, None)
    assert (otro_producto.casos, otro_producto.total) == ((), 0)
    por_codigo = await consultar.bandeja(FiltrosBandeja(q=s05.codigo), 20, None)
    assert [c.decision_id for c in por_codigo.casos] == [s05.id]
    # Los identificadores sintéticos comparten el inicio: se busca con el identificador entero.
    con_prefijo = "COT-" + str(s05.cotizacion_id).upper()
    por_cotizacion = await consultar.bandeja(FiltrosBandeja(q=con_prefijo), 20, None)
    assert [c.decision_id for c in por_cotizacion.casos] == [s05.id]


@pytest.mark.parametrize("cursor", ["%%%", "bm8tdW4tY3Vyc29y", "MjAyNi0xMC0yMlQxNTowMDowMHx4"])
async def test_cursor_invalido(cursor: str, dobles: Any) -> None:
    with pytest.raises(CursorInvalido):
        await _consultar(dobles).bandeja(FiltrosBandeja(), 20, cursor)


async def test_cursor_sin_zona_horaria_es_invalido(dobles: Any, sinteticos: Any) -> None:
    decision = await _decidir(dobles, sinteticos, "S05")
    [caso] = (await _consultar(dobles).bandeja(FiltrosBandeja(), 20, None)).casos
    sin_zona = dataclasses.replace(caso, decidida_en=decision.decidida_en.replace(tzinfo=None))
    with pytest.raises(CursorInvalido):
        decodificar_cursor(codificar_cursor(sin_zona))
    assert decodificar_cursor(codificar_cursor(caso)) == (caso.decidida_en, caso.decision_id)


def test_antiguedad_nunca_negativa(sinteticos: Any) -> None:
    ahora = sinteticos.ahora
    assert antiguedad_minutos(ahora - timedelta(minutes=59, seconds=59), ahora) == 59
    assert antiguedad_minutos(ahora + timedelta(minutes=5), ahora) == 0


async def test_resumen_del_dia(dobles: Any, sinteticos: Any) -> None:
    """DS2-14: resueltas = APROBADA + RECHAZADA del día; fuera del compromiso = en revisión con
    más de 8 h, de cualquier día."""
    await _decidir(dobles, sinteticos, "S01", minutos_antes=10)
    await _decidir(dobles, sinteticos, "S02", minutos_antes=20)
    await _decidir(dobles, sinteticos, "S06", minutos_antes=30)
    await _decidir(dobles, sinteticos, "S05", minutos_antes=60 * 30)  # ayer, fuera del compromiso
    ahora = sinteticos.ahora
    desde = ahora.replace(hour=5, minute=0)  # 00:00 en Bogotá
    resumen = await _consultar(dobles).resumen(desde, desde + timedelta(days=1))
    assert (resumen.resueltas_automaticamente, resumen.enviadas_a_revision, resumen.total) == (
        2,
        1,
        3,
    )
    assert resumen.tiempo_medio_decision_ms is not None
    assert resumen.fuera_del_compromiso == 1
    assert resumen.compromiso_revision_horas == 8


async def test_resumen_sin_decisiones_y_rango_invalido(dobles: Any, sinteticos: Any) -> None:
    ahora = sinteticos.ahora
    vacio = await _consultar(dobles).resumen(ahora - timedelta(hours=1), ahora)
    assert (vacio.total, vacio.tiempo_medio_decision_ms) == (0, None)
    with pytest.raises(RangoInvalido):
        await _consultar(dobles).resumen(ahora, ahora)


async def test_reconstruccion_coincide(id_caso_decidido: str, dobles: Any, sinteticos: Any) -> None:
    decision = await _decidir(dobles, sinteticos, id_caso_decidido)
    reconstruccion = await ReconstruirDecision(dobles.decisiones, dobles.reglas, 300).ejecutar(
        decision.id
    )
    assert reconstruccion.coincide is True
    assert reconstruccion.resultado_original is reconstruccion.resultado_reconstruido
    assert (reconstruccion.version_reglas, reconstruccion.hash_reglas) == (
        "2026.10.1",
        sinteticos.reglas().hash,
    )


async def test_reconstruccion_diferente_si_cambian_las_reglas_o_los_insumos(
    dobles: Any, sinteticos: Any, telemetria: Any
) -> None:
    decision = await _decidir(dobles, sinteticos, "S01")
    reconstruir = ReconstruirDecision(dobles.decisiones, dobles.reglas, 300)
    reglas = sinteticos.reglas()
    dobles.reglas.versiones["2026.10.1"] = dataclasses.replace(reglas, hash="sha256:otra")
    assert (await reconstruir.ejecutar(decision.id)).coincide is False
    endurecidas = tuple(
        dataclasses.replace(r, valor="100000000") if r.codigo == "SUS-004" else r
        for r in reglas.reglas
    )
    dobles.reglas.versiones["2026.10.1"] = dataclasses.replace(reglas, reglas=endurecidas)
    resultado = await reconstruir.ejecutar(decision.id)
    assert resultado.coincide is False
    assert resultado.resultado_reconstruido is ResultadoDecision.REVISION_REQUERIDA
    dobles.reglas.versiones["2026.10.1"] = reglas
    alterada = dataclasses.replace(decision, hash_insumos="sha256:otra")
    dobles.decisiones.decisiones = [alterada]
    assert (await reconstruir.ejecutar(decision.id)).coincide is False
    assert telemetria.resultados("suscripcion.reconstruir") == ["diferente"] * 3


async def test_reconstruccion_sin_decision_o_sin_catalogo(dobles: Any, sinteticos: Any) -> None:
    reconstruir = ReconstruirDecision(dobles.decisiones, dobles.reglas, 50)
    with pytest.raises(DecisionNoEncontrada):
        await reconstruir.ejecutar(uuid4())
    decision = await _decidir(dobles, sinteticos, "S01")
    dobles.reglas.falla = ReglasNoVigentes()
    with pytest.raises(DependenciaNoDisponible):
        await reconstruir.ejecutar(decision.id)
    dobles.reglas.falla = None
    dobles.reglas.demora_s = 0.2
    with pytest.raises(DependenciaNoDisponible) as error:
        await reconstruir.ejecutar(decision.id)
    assert error.value.tiempo_agotado is True
