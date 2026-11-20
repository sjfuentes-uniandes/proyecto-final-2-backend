"""El manejador de `parametricos-eventos` (HU-W21): la evaluación de cada caso del archivo de datos
sobre la copia de coberturas, la evaluación única, lo que se ignora y lo que va a la DLQ."""

import copy
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from pagos_parametricos.adapters.inbound.sqs.eventos import manejador_eventos
from pagos_parametricos.adapters.inbound.sqs.polizas import manejador_polizas
from pagos_parametricos.application.parametrico.aplicar_poliza import AplicarEventoPoliza
from pagos_parametricos.application.parametrico.evaluar_evento import EvaluarEvento
from pagos_parametricos.domain.parametrico.errores import (
    CoberturaInexistente,
    PolizaInexistente,
)
from pagos_parametricos.domain.parametrico.modelos import DecisionEvento, Razon
from solventa_common.inbox import EventoInvalido

pytestmark = pytest.mark.anyio


@pytest.fixture
def manejar(coberturas: Any, evaluaciones: Any, reloj: Any) -> Any:
    return manejador_eventos(EvaluarEvento(coberturas, evaluaciones, reloj))


@pytest.fixture
async def con_coberturas(coberturas: Any, recibido: Any, tx: Any, datos: dict[str, Any]) -> Any:
    aplicar = manejador_polizas(AplicarEventoPoliza(coberturas))
    for entrada in datos["coberturas"]:
        await aplicar(recibido(copy.deepcopy(entrada["sobre"])), tx)
    return coberturas


# --- Los casos del archivo de datos --------------------------------------------------------------
async def test_cada_caso_aceptado_se_evalua_como_dice_el_archivo_de_datos(
    manejar: Any,
    con_coberturas: Any,
    evaluaciones: Any,
    recibido: Any,
    observado: Any,
    tx: Any,
    datos: dict[str, Any],
) -> None:
    claves = {UUID(c["sobre"]["aggregateId"]): c["clave"] for c in datos["coberturas"]}
    evaluados = 0
    for caso in datos["eventos"]:
        esperado = caso["esperado"]["evaluacion"]
        if esperado is None:
            continue
        sobre = observado(caso)
        assert await manejar(recibido(sobre), tx) is None
        registro = evaluaciones.registros[UUID(sobre["eventId"])]
        resultado = registro.resultado
        assert resultado.decision.value == esperado["decision"], caso["clave"]
        assert (resultado.razon.value if resultado.razon else None) == esperado["razonEvento"]
        assert (resultado.evaluadas, resultado.activaciones) == (
            esperado["evaluadas"],
            esperado["activaciones"],
        ), caso["clave"]
        assert {
            claves[e.poliza_id]: {
                "decision": e.decision.value,
                "razon": e.razon.value if e.razon else None,
            }
            for e in resultado.evaluaciones
        } == esperado["porPoliza"], caso["clave"]
        # El origen se conserva y la correlación del sobre llega a lo que se escribe.
        assert registro.evento.fuente == caso["envio"]["fuente"]
        assert registro.evento.id_externo == caso["envio"]["cuerpo"]["idExterno"]
        assert registro.correlation_id == f"corr-{caso['clave']}"
        evaluados += 1
    assert evaluados == 8


async def test_el_valor_y_el_instante_llegan_al_dominio_con_su_tipo(
    manejar: Any,
    con_coberturas: Any,
    evaluaciones: Any,
    recibido: Any,
    observado: Any,
    tx: Any,
    casos: dict[str, dict[str, Any]],
) -> None:
    sobre = observado(casos["valido"])
    await manejar(recibido(sobre), tx)
    evento = evaluaciones.registros[UUID(sobre["eventId"])].evento
    assert evento.valor_observado == Decimal(135) and isinstance(evento.valor_observado, Decimal)
    assert evento.observado_en == datetime(2026, 11, 10, 14, tzinfo=UTC)
    assert (evento.tipo_evento, evento.unidad, evento.zona) == ("LLUVIA_ACUMULADA", "mm", "11001")


# --- Evaluar una sola vez ------------------------------------------------------------------------
async def test_un_evento_ya_evaluado_conserva_su_decision_y_cuenta_el_duplicado(
    manejar: Any,
    con_coberturas: Any,
    evaluaciones: Any,
    coberturas: Any,
    recibido: Any,
    observado: Any,
    tx: Any,
    casos: dict[str, dict[str, Any]],
) -> None:
    sobre = observado(casos["valido"])
    await manejar(recibido(sobre), tx)
    original = evaluaciones.registros[UUID(sobre["eventId"])]
    # Las coberturas cambian y el mismo evento vuelve a llegar (p. ej. tras purgar el inbox).
    for cobertura in list(coberturas.filas.values()):
        coberturas.filas.pop(cobertura.poliza_id)
    await manejar(recibido(sobre), tx)
    assert evaluaciones.registros[UUID(sobre["eventId"])] is original
    assert evaluaciones.duplicados == {UUID(sobre["eventId"]): 1}


# --- Lo que se ignora y lo que va a la DLQ -------------------------------------------------------
async def test_otro_tipo_de_evento_se_ignora(
    manejar: Any, evaluaciones: Any, recibido: Any, tx: Any
) -> None:
    sobre = {"eventId": str(UUID(int=1)), "type": "SiniestroAprobado", "version": 1, "payload": {}}
    assert await manejar(recibido(sobre), tx) is None
    assert evaluaciones.registros == {}


@pytest.mark.parametrize("version", [2, 0, "1", None])
async def test_una_version_distinta_de_1_va_a_la_dlq(
    manejar: Any,
    recibido: Any,
    observado: Any,
    tx: Any,
    casos: dict[str, dict[str, Any]],
    version: Any,
) -> None:
    sobre = observado(casos["valido"])
    sobre["version"] = version
    with pytest.raises(EventoInvalido):
        await manejar(recibido(sobre), tx)


@pytest.mark.parametrize(
    ("cambio", "campo"),
    [
        (lambda p: p.pop("zona"), "zona"),
        (lambda p: p.pop("eventoId"), "eventoId"),
        (lambda p: p.update(tipoEvento="GRANIZO"), "tipoEvento"),
        (lambda p: p.update(valorObservado="mucha"), "valorObservado"),
        (lambda p: p.update(valorObservado=135), "valorObservado"),
        (lambda p: p.update(unidad="km"), "unidad"),
        (lambda p: p.update(observadoEn="2026-11-10T14:00:00"), "observadoEn"),
        (lambda p: p.update(fuente="ideam"), "fuente"),
        (lambda p: p.update(zona="bogota"), "zona"),
    ],
)
async def test_un_evento_que_no_valida_va_a_la_dlq_y_el_aviso_nombra_el_campo_sin_su_valor(
    manejar: Any,
    evaluaciones: Any,
    recibido: Any,
    observado: Any,
    tx: Any,
    casos: dict[str, dict[str, Any]],
    cambio: Any,
    campo: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sobre = observado(casos["valido"])
    cambio(sobre["payload"])
    with caplog.at_level(logging.WARNING), pytest.raises(EventoInvalido):
        await manejar(recibido(sobre), tx)
    aviso = next(r for r in caplog.records if "inválido" in r.getMessage())
    assert [f.removeprefix("payload.") for f in aviso.fields] == [campo]  # type: ignore[attr-defined]
    assert "mucha" not in caplog.text and "bogota" not in caplog.text
    assert evaluaciones.registros == {}


async def test_el_sobre_sin_zona_del_archivo_de_datos_va_a_la_dlq(
    manejar: Any, recibido: Any, tx: Any, datos: dict[str, Any]
) -> None:
    envenenado = next(m for m in datos["mensajesCola"] if m["clave"] == "envenenadoSinZona")
    with pytest.raises(EventoInvalido):
        await manejar(recibido(envenenado["sobre"]), tx)


async def test_la_unidad_incoherente_con_el_tipo_va_a_la_dlq(
    manejar: Any, recibido: Any, observado: Any, tx: Any, casos: dict[str, dict[str, Any]]
) -> None:
    sobre = observado(casos["valido"])
    sobre["payload"].update(tipoEvento="RETRASO_VUELO", unidad="mm", zona="AV204")
    with pytest.raises(EventoInvalido):
        await manejar(recibido(sobre), tx)


async def test_un_event_id_distinto_del_evento_id_va_a_la_dlq(
    manejar: Any, recibido: Any, observado: Any, tx: Any, casos: dict[str, dict[str, Any]]
) -> None:
    """Dos eventId para un mismo evento evaluarían dos veces: la identidad es el `eventoId`."""
    sobre = observado(casos["valido"])
    sobre["eventId"] = str(UUID(int=99))
    with pytest.raises(EventoInvalido):
        await manejar(recibido(sobre), tx)


async def test_los_campos_desconocidos_se_ignoran(
    manejar: Any,
    evaluaciones: Any,
    recibido: Any,
    observado: Any,
    tx: Any,
    casos: dict[str, dict[str, Any]],
) -> None:
    """v1 solo crece de forma aditiva: un campo nuevo del productor no rompe al consumidor."""
    sobre = observado(casos["valido"])
    sobre["payload"]["campoFuturo"] = "x"
    await manejar(recibido(sobre), tx)
    assert len(evaluaciones.registros) == 1


# --- Evaluación dirigida a una póliza (causas de H10) --------------------------------------------
async def test_un_evento_con_una_poliza_inexistente_lanza_poliza_inexistente(
    manejar: Any, recibido: Any, observado: Any, tx: Any, casos: dict[str, dict[str, Any]]
) -> None:
    sobre = observado(casos["valido"])
    sobre["payload"]["polizaId"] = str(UUID(int=12345))
    with pytest.raises(PolizaInexistente):
        await manejar(recibido(sobre), tx)


async def test_una_poliza_cancelada_antes_de_su_emision_lanza_cobertura_inexistente(
    manejar: Any,
    recibido: Any,
    observado: Any,
    tx: Any,
    datos: dict[str, Any],
    casos: dict[str, dict[str, Any]],
    coberturas: Any,
) -> None:
    aplicar = manejador_polizas(AplicarEventoPoliza(coberturas))
    cancelacion = copy.deepcopy(datos["cancelaciones"][0]["sobre"])
    await aplicar(recibido(cancelacion), tx)
    sobre = observado(casos["valido"])
    sobre["payload"]["polizaId"] = cancelacion["aggregateId"]
    with pytest.raises(CoberturaInexistente):
        await manejar(recibido(sobre), tx)


async def test_con_una_poliza_nombrada_salen_las_razones_que_el_indice_no_deja_ver(
    manejar: Any,
    con_coberturas: Any,
    evaluaciones: Any,
    coberturas: Any,
    recibido: Any,
    observado: Any,
    tx: Any,
    datos: dict[str, Any],
    casos: dict[str, dict[str, Any]],
) -> None:
    por_clave = {c["clave"]: UUID(c["sobre"]["aggregateId"]) for c in datos["coberturas"]}
    # La póliza de Barranquilla (08001) para un evento de Bogotá (11001), dentro de su ventana:
    # FUERA_DE_ZONA, una razón que las candidatas por índice nunca producen.
    sobre = observado(casos["valido"])
    sobre["payload"]["polizaId"] = str(por_clave["COB-BAQ-1"])
    await manejar(recibido(sobre), tx)
    [evaluacion] = evaluaciones.registros[UUID(sobre["eventId"])].resultado.evaluaciones
    assert evaluacion.razon is Razon.FUERA_DE_ZONA
    # Una póliza cancelada: POLIZA_NO_ACTIVA.
    aplicar = manejador_polizas(AplicarEventoPoliza(coberturas))
    cancelacion = copy.deepcopy(datos["cancelaciones"][0]["sobre"])
    cancelacion["aggregateId"] = cancelacion["payload"]["polizaId"] = str(por_clave["COB-BOG-1"])
    cancelacion["occurredAt"] = "2026-12-01T00:00:00Z"
    await aplicar(recibido(cancelacion), tx)
    otro = observado(casos["justoEnElUmbral"])
    otro["payload"]["polizaId"] = str(por_clave["COB-BOG-1"])
    await manejar(recibido(otro), tx)
    resultado = evaluaciones.registros[UUID(otro["eventId"])].resultado
    assert resultado.decision is DecisionEvento.NO_ACTIVA
    assert resultado.evaluaciones[0].razon is Razon.POLIZA_NO_ACTIVA


# --- Medición y logs -----------------------------------------------------------------------------
async def test_la_medicion_por_decision_y_el_log_sin_valores(
    manejar: Any,
    con_coberturas: Any,
    recibido: Any,
    observado: Any,
    tx: Any,
    casos: dict[str, dict[str, Any]],
    telemetria: Any,
) -> None:
    esperados = {
        "valido": "activa",
        "unaUnidadBajoElUmbral": "no_activa",
        "fueraDeZona": "sin_polizas",
    }
    for clave in esperados:
        await manejar(recibido(observado(casos[clave])), tx)
    medidas = [
        r
        for r in telemetria.logs()
        if r.get("operation") == "parametrico.evaluar" and r.get("result")
    ]
    assert [m["result"] for m in medidas] == list(esperados.values())
    texto = telemetria.texto_logs()
    for prohibido in ("IDEAM-2026-1110-0001", "EVT-2026-0913", "1400000", '"135"'):
        assert prohibido not in texto
    cierre = next(r for r in telemetria.logs() if r["message"] == "parametrico: evento evaluado")
    assert cierre["decision"] == "ACTIVA" and cierre["fuente"] == "IDEAM-SIM"
    assert (cierre["evaluadas"], cierre["activaciones"]) == (2, 1)


async def test_un_duplicado_se_mide_como_duplicado(
    manejar: Any,
    con_coberturas: Any,
    recibido: Any,
    observado: Any,
    tx: Any,
    casos: dict[str, dict[str, Any]],
    telemetria: Any,
) -> None:
    sobre = observado(casos["valido"])
    await manejar(recibido(sobre), tx)
    await manejar(recibido(sobre), tx)
    medidas = [
        r
        for r in telemetria.logs()
        if r.get("operation") == "parametrico.evaluar" and r.get("result")
    ]
    assert [m["result"] for m in medidas] == ["activa", "duplicado"]


async def test_sin_correlacion_en_el_sobre_se_genera_una(
    manejar: Any,
    evaluaciones: Any,
    recibido: Any,
    observado: Any,
    tx: Any,
    casos: dict[str, dict[str, Any]],
) -> None:
    sobre = observado(casos["valido"])
    del sobre["correlationId"]
    await manejar(recibido(sobre), tx)
    [registro] = evaluaciones.registros.values()
    assert registro.correlation_id
    assert json.loads(json.dumps(registro.correlation_id)) == registro.correlation_id
