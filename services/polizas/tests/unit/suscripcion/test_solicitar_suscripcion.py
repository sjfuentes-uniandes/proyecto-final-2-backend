"""UT-W13-03: caso de uso solicitar_suscripcion con dobles de los puertos (HU-W13)."""

import dataclasses
import itertools
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from polizas.application.suscripcion.solicitar_suscripcion import (
    ComandoSuscripcion,
    ConfigSuscripcion,
    SolicitarSuscripcion,
)
from polizas.domain.suscripcion.errores import (
    ConflictoIdempotencia,
    CotizacionNoEncontrada,
    DependenciaNoDisponible,
    IdentidadNoVerificada,
    IdentidadPendiente,
    OfertaVencida,
    ReglasNoVigentes,
)
from polizas.domain.suscripcion.modelos import Actor, EstadoIdentidad, ResultadoDecision, RolActor
from solventa_common import telemetry

pytestmark = pytest.mark.anyio
ASESOR = Actor("sub-asesor-1", RolActor.ASESOR)


def _caso_uso(dobles: Any, **config: int) -> SolicitarSuscripcion:
    return SolicitarSuscripcion(
        dobles.insumos,
        dobles.reglas,
        dobles.decisiones,
        dobles.codigos,
        dobles.reloj,
        ConfigSuscripcion(**config),
    )


def _comando(sinteticos: Any, id_caso: str, clave: UUID | None = None) -> ComandoSuscripcion:
    return ComandoSuscripcion(
        cotizacion_id=UUID(sinteticos.cotizacion_id(id_caso)),
        idempotency_key=clave or uuid4(),
        actor=ASESOR,
        correlation_id="corr-ut-w13",
    )


async def test_decide_y_guarda_cada_caso_sintetico(
    id_caso_decidido: str, dobles: Any, sinteticos: Any
) -> None:
    decision, creada = await _caso_uso(dobles).ejecutar(_comando(sinteticos, id_caso_decidido))
    esperado = sinteticos.caso(id_caso_decidido)["esperado"]
    assert creada is True
    assert decision.resultado.value == esperado["resultado"]
    assert decision.version_reglas == "2026.10.1"
    assert decision.hash_reglas == sinteticos.reglas().hash
    assert decision.codigo == "DEC-2026-000001"
    assert decision.decidida_en == sinteticos.ahora
    assert decision.actor_id == ASESOR.id
    assert dobles.decisiones.decisiones == [decision]
    assert dobles.insumos.llamadas == [(decision.cotizacion_id, ASESOR)]


async def test_misma_clave_devuelve_la_misma_decision(dobles: Any, sinteticos: Any) -> None:
    caso_uso, clave = _caso_uso(dobles), uuid4()
    primera, _ = await caso_uso.ejecutar(_comando(sinteticos, "S01", clave))
    repetida, creada = await caso_uso.ejecutar(_comando(sinteticos, "S01", clave))
    assert (repetida, creada) == (primera, False)
    assert len(dobles.insumos.llamadas) == 1  # no vuelve a consultar


async def test_otra_clave_sobre_la_misma_cotizacion_no_crea_otra_decision(
    dobles: Any, sinteticos: Any
) -> None:
    """DS2-05: una sola decisión por cotización, nunca contradictoria."""
    caso_uso = _caso_uso(dobles)
    primera, _ = await caso_uso.ejecutar(_comando(sinteticos, "S01"))
    otra, creada = await caso_uso.ejecutar(_comando(sinteticos, "S01"))
    assert (otra, creada) == (primera, False)
    assert len(dobles.decisiones.decisiones) == 1


async def test_misma_clave_con_otra_cotizacion_es_conflicto(dobles: Any, sinteticos: Any) -> None:
    caso_uso, clave = _caso_uso(dobles), uuid4()
    await caso_uso.ejecutar(_comando(sinteticos, "S01", clave))
    with pytest.raises(ConflictoIdempotencia):
        await caso_uso.ejecutar(_comando(sinteticos, "S02", clave))


@pytest.mark.parametrize(
    ("sin_decision", "error"),
    [("S10", IdentidadNoVerificada), ("S11", IdentidadPendiente), ("S09", OfertaVencida)],
)
async def test_precondiciones_sin_decision(
    sin_decision: str, error: type[Exception], dobles: Any, sinteticos: Any
) -> None:
    with pytest.raises(error):
        await _caso_uso(dobles).ejecutar(_comando(sinteticos, sin_decision))
    assert dobles.decisiones.decisiones == []


@pytest.mark.parametrize(
    ("desfase", "vigente"),
    [(-timedelta(microseconds=1), True), (timedelta(0), False), (timedelta(microseconds=1), False)],
)
async def test_vigencia_en_los_limites_con_el_reloj(
    desfase: timedelta, vigente: bool, dobles: Any, sinteticos: Any
) -> None:
    """DS2-10: el instante exacto vigenteHasta ya es vencido."""
    base = sinteticos.insumos("S01")
    dobles.reloj.instante = base.vigente_hasta + desfase
    if vigente:
        decision, _ = await _caso_uso(dobles).ejecutar(_comando(sinteticos, "S01"))
        assert decision.resultado is ResultadoDecision.APROBADA
    else:
        with pytest.raises(OfertaVencida):
            await _caso_uso(dobles).ejecutar(_comando(sinteticos, "S01"))


async def test_la_identidad_se_verifica_antes_que_la_vigencia(dobles: Any, sinteticos: Any) -> None:
    vencida_y_pendiente = dataclasses.replace(
        sinteticos.insumos("S09"), estado_identidad=EstadoIdentidad.PENDIENTE
    )
    dobles.insumos.casos[vencida_y_pendiente.cotizacion_id] = vencida_y_pendiente
    with pytest.raises(IdentidadPendiente):
        await _caso_uso(dobles).ejecutar(_comando(sinteticos, "S09"))


async def test_cotizacion_inexistente_y_reglas_no_vigentes(dobles: Any, sinteticos: Any) -> None:
    inexistente = ComandoSuscripcion(uuid4(), uuid4(), ASESOR, "c")
    with pytest.raises(CotizacionNoEncontrada):
        await _caso_uso(dobles).ejecutar(inexistente)
    dobles.reglas.vigente = None
    with pytest.raises(ReglasNoVigentes):
        await _caso_uso(dobles).ejecutar(_comando(sinteticos, "S01"))
    assert dobles.decisiones.decisiones == []


@pytest.mark.parametrize("dependencia", ["cotizacion", "catalogo"])
async def test_dependencia_caida_es_falla_cerrada(
    dependencia: str, dobles: Any, sinteticos: Any
) -> None:
    doble = dobles.insumos if dependencia == "cotizacion" else dobles.reglas
    doble.falla = DependenciaNoDisponible(dependencia)
    with pytest.raises(DependenciaNoDisponible) as error:
        await _caso_uso(dobles).ejecutar(_comando(sinteticos, "S01"))
    assert (error.value.dependencia, error.value.tiempo_agotado) == (dependencia, False)
    assert dobles.decisiones.decisiones == []


@pytest.mark.parametrize("dependencia", ["cotizacion", "catalogo"])
async def test_dependencia_lenta_agota_su_timeout(
    dependencia: str, dobles: Any, sinteticos: Any
) -> None:
    """DS2-19: cada dependencia tiene su timeout; pasado, falla cerrada sin decisión."""
    doble = dobles.insumos if dependencia == "cotizacion" else dobles.reglas
    doble.demora_s = 0.2
    caso_uso = _caso_uso(dobles, cotizacion_timeout_ms=50, catalogo_timeout_ms=50)
    with pytest.raises(DependenciaNoDisponible) as error:
        await caso_uso.ejecutar(_comando(sinteticos, "S01"))
    assert (error.value.dependencia, error.value.tiempo_agotado) == (dependencia, True)
    assert dobles.decisiones.decisiones == []


async def test_carrera_devuelve_la_decision_ganadora(dobles: Any, sinteticos: Any) -> None:
    """Otra solicitud guardó primero (ON CONFLICT DO NOTHING): se devuelve la ganadora."""
    ganadora, _ = await _caso_uso(dobles).ejecutar(_comando(sinteticos, "S01"))
    perdedora = _caso_uso(dobles)
    # Simula que la ganadora aún no era visible al empezar y que el insert pierde la carrera.
    visibles = list(dobles.decisiones.decisiones)
    dobles.decisiones.decisiones = []
    original_guardar = dobles.decisiones.guardar

    async def guardar_tarde(decision: Any) -> bool:
        dobles.decisiones.decisiones = visibles
        return await original_guardar(decision)

    dobles.decisiones.guardar = guardar_tarde
    decision, creada = await perdedora.ejecutar(_comando(sinteticos, "S01"))
    assert (decision, creada) == (ganadora, False)


async def test_carrera_sin_ganadora_visible_es_conflicto(dobles: Any, sinteticos: Any) -> None:
    dobles.decisiones.guardar_falla = True
    with pytest.raises(ConflictoIdempotencia):
        await _caso_uso(dobles).ejecutar(_comando(sinteticos, "S01"))


async def test_carrera_resuelta_por_la_clave(dobles: Any, sinteticos: Any) -> None:
    """La ganadora usó la misma clave para la misma cotización y aún no es visible por
    cotización: se encuentra por la clave."""
    clave = uuid4()
    ganadora, _ = await _caso_uso(dobles).ejecutar(_comando(sinteticos, "S01", clave))
    repo = dobles.decisiones
    llamadas = {"n": 0}
    original = repo.por_cotizacion

    async def por_cotizacion_tardia(cotizacion_id: UUID) -> Any:
        llamadas["n"] += 1
        return None if llamadas["n"] <= 2 else await original(cotizacion_id)

    original_clave = repo.por_idempotencia
    visto = {"n": 0}

    async def por_clave_tardia(clave_: UUID) -> Any:
        visto["n"] += 1
        return None if visto["n"] == 1 else await original_clave(clave_)

    repo.por_cotizacion = por_cotizacion_tardia
    repo.por_idempotencia = por_clave_tardia
    decision, creada = await _caso_uso(dobles).ejecutar(_comando(sinteticos, "S01", clave))
    assert (decision, creada) == (ganadora, False)


async def test_mide_y_registra_sin_pii(dobles: Any, sinteticos: Any, telemetria: Any) -> None:
    """SEC-W13-01: el log de cierre lleva solo identificadores; las operaciones se miden con el
    resultado de la decisión o de la falla."""
    caso_uso = _caso_uso(dobles)
    decision, _ = await caso_uso.ejecutar(_comando(sinteticos, "S06"))
    with pytest.raises(OfertaVencida):
        await caso_uso.ejecutar(_comando(sinteticos, "S09"))
    dobles.insumos.falla = DependenciaNoDisponible("cotizacion")
    with pytest.raises(DependenciaNoDisponible):
        await caso_uso.ejecutar(_comando(sinteticos, "S01"))
    assert telemetria.resultados("suscripcion.evaluar") == ["revision", "rechazado", "error"]
    assert telemetria.resultados("suscripcion.dependencia.cotizacion.insumos") == [
        "ok",
        "ok",
        "error",
    ]
    assert telemetria.resultados("suscripcion.dependencia.catalogo.reglas") == ["ok"]
    assert telemetria.mediciones("suscripcion.evaluar") == 3
    [cierre] = [log for log in telemetria.logs() if log["message"] == "suscripcion decidida"]
    assert cierre["decision_id"] == str(decision.id)
    assert cierre["resultado"] == "REVISION_REQUERIDA"
    assert [d["nombre"] for d in cierre["dependencias"]] == [
        "cotizacion.insumos",
        "catalogo.reglas",
    ]
    _comprobar_sin_pii(telemetria)


_SENSIBLES = ("620000000", "1990-04-18", "sumaAsegurada", "0.92", '"edad"')


# Hora de `FormateadorJson`: se quita y la fecha se conserva.
_HORA = re.compile(r"(\d{4}-\d{2}-\d{2})T\d{2}:\d{2}:\d{2}\.\d{3}\+00:00")


def _sin_mediciones(log: dict[str, Any]) -> dict[str, Any]:
    """El log sin lo que mide el reloj (la duración y la hora): un número medido puede coincidir
    con un centinela sin ser una fuga (16.1b). La duración solo se quita en los registros del
    logger de `telemetry.registrar`. Lo demás, claves incluidas, se conserva."""
    duracion, hora = log.get("duration_ms"), _HORA.fullmatch(str(log.get("timestamp")))
    medida = log.get("logger") == "solventa.telemetria" and isinstance(duracion, int | float)
    return {
        **log,
        "duration_ms": None if medida else duracion,
        "timestamp": hora.group(1) if hora else log.get("timestamp"),
    }


def _comprobar_sin_pii(telemetria: Any) -> None:
    logs = (json.dumps(_sin_mediciones(log), ensure_ascii=False) for log in telemetria.logs())
    texto = "\n".join(logs)
    for sensible in _SENSIBLES:
        assert sensible not in texto, sensible


def _medir_como(monkeypatch: pytest.MonkeyPatch, medicion: str, valor: str) -> str:
    """Fija el reloj para que `medicion` valga `valor` en los logs; devuelve el texto esperado."""
    if medicion == "duration_ms":
        # perf_counter de la telemetría a pasos fijos: la operación sin otras dentro mide `valor`.
        pasos, paso = itertools.count(), float(valor) / 1000
        monkeypatch.setattr(
            telemetry, "time", SimpleNamespace(perf_counter=lambda: next(pasos) * paso)
        )
        return f'"duration_ms": {valor}'
    # Hora del registro «…:10.<decimales>0»: el segundo 10 y los milisegundos forman `valor`.
    instante = datetime(2026, 10, 28, 15, 9, 10, int(float(valor) * 1e6) + 500, UTC).timestamp()
    monkeypatch.setattr(
        logging, "time", SimpleNamespace(time=lambda: instante, time_ns=lambda: int(instante * 1e9))
    )
    return f":10.{valor[2:]}0+00:00"


@pytest.mark.parametrize("medicion", ["duration_ms", "timestamp"])
async def test_una_medicion_igual_a_un_centinela_no_es_fuga(
    medicion: str, dobles: Any, sinteticos: Any, telemetria: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """16.1b: la duración o la hora de un log pueden valer «0.92», la confianza de S06, sin que
    sea una fuga."""
    esperado = _medir_como(monkeypatch, medicion, "0.92")
    await _caso_uso(dobles).ejecutar(_comando(sinteticos, "S06"))
    assert esperado in telemetria.texto_logs()  # la coincidencia está armada
    _comprobar_sin_pii(telemetria)


async def test_una_duracion_fuera_de_la_telemetria_es_fuga(telemetria: Any) -> None:
    """16.1b: solo se ignora la duración de `solventa.telemetria`; en otro logger, un
    `duration_ms` igual al centinela es una fuga."""
    logging.getLogger("polizas.suscripcion").info("fuga", extra={"duration_ms": 0.92})
    with pytest.raises(AssertionError, match="0.92"):
        _comprobar_sin_pii(telemetria)


async def test_la_decision_existente_mide_su_resultado(
    dobles: Any, sinteticos: Any, telemetria: Any
) -> None:
    caso_uso = _caso_uso(dobles)
    await caso_uso.ejecutar(_comando(sinteticos, "S02"))
    await caso_uso.ejecutar(_comando(sinteticos, "S02"))
    assert telemetria.resultados("suscripcion.evaluar") == ["rechazada", "rechazada"]


async def test_timeout_se_mide_como_timeout(dobles: Any, sinteticos: Any, telemetria: Any) -> None:
    dobles.reglas.demora_s = 0.2
    with pytest.raises(DependenciaNoDisponible):
        await _caso_uso(dobles, catalogo_timeout_ms=20).ejecutar(_comando(sinteticos, "S01"))
    assert telemetria.resultados("suscripcion.evaluar") == ["timeout"]
    assert telemetria.resultados("suscripcion.dependencia.catalogo.reglas") == ["timeout"]
