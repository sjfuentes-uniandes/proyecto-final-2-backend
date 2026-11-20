"""La tarea de plazos del soporte (HU-W20, DS3-13 v1.3): el caso vuelve al mismo analista."""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID

import pytest
from siniestros.application.siniestro import plazos as modulo
from siniestros.application.siniestro.plazos import (
    ACCION_PLAZO_SOPORTE_VENCIDO,
    VencerPlazosSoporte,
)
from siniestros.domain.siniestro.modelos import EstadoSiniestro, Siniestro
from siniestros.domain.siniestro.plazos import tiempo_restante
from solventa_common import correlation

pytestmark = pytest.mark.anyio
ANALISTA = UUID("a0a0a0a0-0000-4000-8000-000000000001")
HORA = timedelta(hours=1)
DIA = timedelta(days=1)


@pytest.fixture
def en_soporte(siniestro: Callable[..., Siniestro], ahora: datetime) -> Callable[..., Siniestro]:
    """Un caso FALTA_SOPORTE pausado hace 5 días, con el plazo vencido hace `vencido_hace`."""

    def fabricar(vencido_hace: timedelta = HORA, **cambios: object) -> Siniestro:
        datos: dict[str, object] = {
            "estado": EstadoSiniestro.FALTA_SOPORTE,
            "analista_id": ANALISTA,
            "asignado_en": ahora - 6 * DIA,
            "reportado_en": ahora - 6 * DIA,
            "fecha_evento": ahora - 6 * DIA - HORA,
            "pausado_desde": ahora - 5 * DIA,
            "pausa_acumulada_s": 100,
            "plazo_soporte_vence_en": ahora - vencido_hace,
        }
        datos.update(cambios)
        return siniestro(**datos)

    return fabricar


@pytest.fixture
def tarea(repo, reloj):  # type: ignore[no-untyped-def]
    return VencerPlazosSoporte(repo, reloj, lote=10)


async def test_el_caso_vuelve_a_evaluacion_con_el_mismo_analista_y_el_reloj_reanudado(
    tarea,
    repo,
    en_soporte,
    ahora,  # type: ignore[no-untyped-def]
) -> None:
    caso = en_soporte(correlation_id="corr-del-caso")
    repo.agregar(caso)
    assert await tarea.pasada() == 1
    devuelto = repo.siniestros[caso.id]
    assert devuelto.estado is EstadoSiniestro.EN_EVALUACION
    assert devuelto.analista_id == ANALISTA and devuelto.asignado_en == caso.asignado_en
    assert devuelto.pausado_desde is None and devuelto.plazo_soporte_vence_en is None
    assert devuelto.pausa_acumulada_s == 100 + int((5 * DIA).total_seconds())
    # El reloj sigue donde estaba al pausar: 6 días reportado, 5 en pausa → ya venció el
    # compromiso de 48 h (−72 h + 5 días de pausa = 1 día corrido; quedan 24 h).
    assert tiempo_restante(devuelto, ahora) == tiempo_restante(caso, ahora)

    (entrada,) = repo.historial
    assert (entrada.accion, entrada.estado_anterior, entrada.estado_nuevo) == (
        ACCION_PLAZO_SOPORTE_VENCIDO,
        EstadoSiniestro.FALTA_SOPORTE,
        EstadoSiniestro.EN_EVALUACION,
    )
    assert (entrada.actor_rol, entrada.actor_id, entrada.actor_nombre) == ("SISTEMA", None, None)
    assert entrada.justificacion is None
    assert entrada.detalles == {"plazoVenceEn": caso.plazo_soporte_vence_en.isoformat()}
    assert repo.eventos == [("SiniestroEnEvaluacion", caso.id, ahora, "PLAZO_SOPORTE_VENCIDO")]
    assert repo.transacciones == 1


async def test_no_toca_los_casos_cuyo_plazo_no_ha_vencido_ni_los_de_otro_estado(
    tarea,
    repo,
    en_soporte,
    siniestro,
    ahora,  # type: ignore[no-untyped-def]
) -> None:
    vigente = en_soporte(vencido_hace=-HORA, codigo="88-2026-0501")
    justo = en_soporte(vencido_hace=timedelta(0), codigo="88-2026-0502")
    asignado = siniestro(
        codigo="88-2026-0503",
        estado=EstadoSiniestro.EN_EVALUACION,
        analista_id=ANALISTA,
        asignado_en=ahora,
    )
    repo.agregar(vigente)
    repo.agregar(justo)
    repo.agregar(asignado)
    assert await tarea.pasada() == 0
    assert repo.siniestros[vigente.id] == vigente and repo.siniestros[justo.id] == justo
    assert repo.historial == [] and repo.eventos == []


async def test_procesa_en_lotes_hasta_vaciar_los_vencidos(
    repo,
    reloj,
    en_soporte,  # type: ignore[no-untyped-def]
) -> None:
    for n in range(5):
        repo.agregar(en_soporte(vencido_hace=HORA * (n + 1), codigo=f"88-2026-060{n}"))
    tarea = VencerPlazosSoporte(repo, reloj, lote=2)
    assert await tarea.pasada() == 5
    # Lotes de 2, 2 y 1: la transacción que trae menos que el lote es la última.
    assert repo.transacciones == 3
    assert all(s.estado is EstadoSiniestro.EN_EVALUACION for s in repo.siniestros.values())
    assert len(repo.historial) == 5 and len(repo.eventos) == 5


async def test_un_lote_exacto_termina_con_una_transaccion_vacia(
    repo,
    reloj,
    en_soporte,  # type: ignore[no-untyped-def]
) -> None:
    for n in range(2):
        repo.agregar(en_soporte(codigo=f"88-2026-070{n}"))
    assert await VencerPlazosSoporte(repo, reloj, lote=2).pasada() == 2
    assert repo.transacciones == 2


async def test_el_mas_antiguo_va_primero(repo, reloj, en_soporte) -> None:  # type: ignore[no-untyped-def]
    reciente = en_soporte(vencido_hace=HORA, codigo="88-2026-0801")
    antiguo = en_soporte(vencido_hace=3 * DIA, codigo="88-2026-0802")
    repo.agregar(reciente)
    repo.agregar(antiguo)
    await VencerPlazosSoporte(repo, reloj, lote=1).pasada()
    assert [e[1] for e in repo.eventos] == [antiguo.id, reciente.id]


async def test_la_correlacion_del_caso_se_restablece_al_terminar(
    tarea,
    repo,
    en_soporte,  # type: ignore[no-untyped-def]
) -> None:
    antes = correlation.obtener()
    repo.agregar(en_soporte(correlation_id="corr-del-caso"))
    await tarea.pasada()
    assert correlation.obtener() == antes


async def test_si_el_lote_falla_no_queda_nada_a_medias(
    repo,
    reloj,
    en_soporte,
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
) -> None:
    caso = en_soporte()
    repo.agregar(caso)

    async def falla(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("la base se cayó")

    async with repo.transaccion() as tx:
        monkeypatch.setattr(type(tx), "evento_en_evaluacion", falla)
    with pytest.raises(RuntimeError):
        await VencerPlazosSoporte(repo, reloj, lote=10).pasada()
    assert repo.siniestros[caso.id] == caso
    assert repo.historial == [] and repo.eventos == []


async def test_la_pasada_se_mide_y_se_registra_sin_datos_del_caso(
    tarea,
    repo,
    en_soporte,
    caplog: pytest.LogCaptureFixture,  # type: ignore[no-untyped-def]
) -> None:
    repo.agregar(en_soporte())
    with caplog.at_level(logging.INFO):
        await tarea.pasada()
    cierre = next(r for r in caplog.records if r.getMessage() == modulo.MENSAJE_PASADA)
    assert cierre.vencidos == 1  # type: ignore[attr-defined]
    medida = next(r for r in caplog.records if getattr(r, "operation", None) == "siniestro.plazos")
    assert medida.result == "ok"  # type: ignore[attr-defined]


# --- El bucle ----------------------------------------------------------------------------------------


async def test_con_la_parada_ya_puesta_no_hace_ninguna_pasada(
    tarea,
    repo,
    en_soporte,  # type: ignore[no-untyped-def]
) -> None:
    repo.agregar(en_soporte())
    parada = asyncio.Event()
    parada.set()
    await tarea.ejecutar(parada, intervalo_s=60)
    assert repo.transacciones == 0


async def test_el_bucle_espera_el_intervalo_pasa_y_se_detiene_con_la_parada(
    tarea,
    repo,
    en_soporte,  # type: ignore[no-untyped-def]
) -> None:
    repo.agregar(en_soporte())
    parada = asyncio.Event()
    corriendo = asyncio.create_task(tarea.ejecutar(parada, intervalo_s=0.01))
    for _ in range(200):
        if repo.historial:
            break
        await asyncio.sleep(0.01)
    parada.set()
    await asyncio.wait_for(corriendo, 2)
    assert len(repo.historial) == 1


async def test_una_pasada_que_falla_se_registra_y_el_bucle_sigue(
    tarea,
    repo,
    en_soporte,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,  # type: ignore[no-untyped-def]
) -> None:
    repo.agregar(en_soporte())
    llamadas = 0
    original = tarea.pasada

    async def a_veces_falla() -> int:
        nonlocal llamadas
        llamadas += 1
        if llamadas == 1:
            raise RuntimeError("DATO-PRIVADO")
        return await original()

    monkeypatch.setattr(tarea, "pasada", a_veces_falla)
    parada = asyncio.Event()
    with caplog.at_level(logging.ERROR):
        corriendo = asyncio.create_task(tarea.ejecutar(parada, intervalo_s=0.01))
        for _ in range(300):
            if repo.historial:
                break
            await asyncio.sleep(0.01)
        parada.set()
        await asyncio.wait_for(corriendo, 2)
    assert llamadas >= 2 and len(repo.historial) == 1
    fallo = next(r for r in caplog.records if r.getMessage() == modulo.MENSAJE_PASADA_FALLIDA)
    assert fallo.error_type == "RuntimeError"  # type: ignore[attr-defined]
    assert "DATO-PRIVADO" not in caplog.text
