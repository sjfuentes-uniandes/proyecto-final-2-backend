"""El manejador de `parametricos-polizas` (HU-W21, DS3-22): qué cobertura sale de cada evento, qué
se ignora, qué va a la DLQ y el orden de llegada, con los escenarios del archivo de datos."""

import copy
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from pagos_parametricos.adapters.inbound.sqs.polizas import manejador_polizas
from pagos_parametricos.application.parametrico.aplicar_poliza import AplicarEventoPoliza
from pagos_parametricos.domain.parametrico.modelos import EstadoCobertura
from solventa_common.inbox import EventoInvalido

pytestmark = pytest.mark.anyio


@pytest.fixture
def manejar(coberturas: Any) -> Any:
    return manejador_polizas(AplicarEventoPoliza(coberturas))


def _sobre(datos: dict[str, Any], clave: str = "COB-BOG-1") -> dict[str, Any]:
    return copy.deepcopy(next(c["sobre"] for c in datos["coberturas"] if c["clave"] == clave))


def _cancelacion(datos: dict[str, Any], clave: str) -> dict[str, Any]:
    return copy.deepcopy(next(c["sobre"] for c in datos["cancelaciones"] if c["clave"] == clave))


# --- PolizaEmitida con cobertura -----------------------------------------------------------------
async def test_la_emision_con_cobertura_crea_la_copia_con_la_ventana_de_su_vigencia(
    manejar: Any, coberturas: Any, recibido: Any, tx: Any, datos: dict[str, Any]
) -> None:
    sobre = _sobre(datos)
    assert await manejar(recibido(sobre), tx) is None
    cobertura = coberturas.filas[UUID(sobre["payload"]["polizaId"])]
    regla = cobertura.regla
    assert cobertura.estado is EstadoCobertura.ACTIVA
    assert cobertura.numero == sobre["payload"]["numero"]
    assert (regla.tipo_evento, regla.zona, regla.unidad, regla.moneda) == (
        "LLUVIA_ACUMULADA",
        "11001",
        "mm",
        "COP",
    )
    assert (regla.umbral, regla.valor_pago, regla.version_regla) == (
        Decimal(120),
        Decimal("1400000.00"),
        "1",
    )
    # Vigencia 2026-09-01 a 2027-08-31, inclusiva, en hora de Colombia.
    assert regla.vigente_desde == datetime(2026, 9, 1, 5, tzinfo=UTC)
    assert regla.vigente_hasta == datetime(2027, 9, 1, 5, tzinfo=UTC)
    assert cobertura.ultimo_evento_en == datetime.fromisoformat(sobre["occurredAt"])
    assert cobertura.aggregate_version is None
    assert coberturas.bloqueadas == [UUID(sobre["payload"]["polizaId"])]


async def test_cada_cobertura_sintetica_del_archivo_de_datos_se_aplica(
    manejar: Any, coberturas: Any, recibido: Any, tx: Any, datos: dict[str, Any]
) -> None:
    for entrada in datos["coberturas"]:
        await manejar(recibido(entrada["sobre"]), tx)
    assert len(coberturas.filas) == len(datos["coberturas"]) == 10


async def test_el_aggregate_version_del_sobre_se_guarda(
    manejar: Any, coberturas: Any, recibido: Any, tx: Any, datos: dict[str, Any]
) -> None:
    sobre = _sobre(datos, "COB-ORD-3")
    await manejar(recibido(sobre), tx)
    assert coberturas.filas[UUID(sobre["aggregateId"])].aggregate_version == 1


# --- Lo que se ignora ----------------------------------------------------------------------------
async def test_una_emision_sin_cobertura_parametrica_se_ignora(
    manejar: Any, coberturas: Any, recibido: Any, tx: Any, datos: dict[str, Any]
) -> None:
    for variante in ("sin_campo", "nula"):
        sobre = _sobre(datos)
        if variante == "sin_campo":
            del sobre["payload"]["coberturaParametrica"]
        else:
            sobre["payload"]["coberturaParametrica"] = None
        assert await manejar(recibido(sobre), tx) is None
    assert coberturas.filas == {} and coberturas.bloqueadas == []


@pytest.mark.parametrize("tipo", ["PolizaVencida", "PolizaRenovada", "PolizaEmitidaV2"])
async def test_los_demas_poliza_se_ignoran_aunque_su_cuerpo_sea_otro(
    manejar: Any, coberturas: Any, recibido: Any, tx: Any, tipo: str
) -> None:
    sobre = {
        "eventId": str(UUID(int=1)),
        "type": tipo,
        "version": 9,
        "occurredAt": "2026-11-10T10:00:00Z",
        "payload": {"cualquier": "cosa"},
    }
    assert await manejar(recibido(sobre), tx) is None
    assert coberturas.filas == {}


async def test_lo_ignorado_se_mide_como_ignorada(
    manejar: Any, recibido: Any, tx: Any, datos: dict[str, Any], telemetria: Any
) -> None:
    sobre = _sobre(datos)
    del sobre["payload"]["coberturaParametrica"]
    await manejar(recibido(sobre), tx)
    medicion = [r for r in telemetria.logs() if r.get("operation") == "parametrico.coberturas"]
    assert medicion[-1]["result"] == "ignorada"


# --- PolizaCancelada y orden ---------------------------------------------------------------------
async def test_la_cancelacion_deja_la_cobertura_cancelada_con_su_regla(
    manejar: Any, coberturas: Any, recibido: Any, tx: Any, datos: dict[str, Any]
) -> None:
    emitida = _sobre(datos, "COB-ORD-2")
    await manejar(recibido(emitida), tx)
    await manejar(recibido(_cancelacion(datos, "CAN-ORD-2")), tx)
    cobertura = coberturas.filas[UUID(emitida["aggregateId"])]
    assert cobertura.estado is EstadoCobertura.CANCELADA
    assert cobertura.regla is not None and cobertura.numero == emitida["payload"]["numero"]


async def test_los_escenarios_de_orden_del_archivo_de_datos(
    coberturas: Any, recibido: Any, tx: Any, datos: dict[str, Any]
) -> None:
    sobres = {c["clave"]: c["sobre"] for c in [*datos["coberturas"], *datos["cancelaciones"]]}
    assert len(datos["ordenCoberturas"]) == 5
    for escenario in datos["ordenCoberturas"]:
        manejar = manejador_polizas(AplicarEventoPoliza(coberturas))
        for clave in escenario["llegada"]:
            await manejar(recibido(copy.deepcopy(sobres[clave])), tx)
        poliza = UUID(sobres[escenario["esperado"]["cobertura"]]["aggregateId"])
        assert coberturas.filas[poliza].estado.value == escenario["esperado"]["estado"], escenario[
            "clave"
        ]


async def test_una_emision_atrasada_se_confirma_y_se_mide_como_atrasada(
    manejar: Any, coberturas: Any, recibido: Any, tx: Any, datos: dict[str, Any], telemetria: Any
) -> None:
    await manejar(recibido(_cancelacion(datos, "CAN-ORD-2")), tx)  # 11:00
    await manejar(recibido(_sobre(datos, "COB-ORD-2")), tx)  # 10:00, atrasada
    [fila] = coberturas.filas.values()
    assert fila.estado is EstadoCobertura.CANCELADA and fila.regla is None
    medicion = [r for r in telemetria.logs() if r.get("operation") == "parametrico.coberturas"]
    assert [r["result"] for r in medicion] == ["aplicada", "atrasada"]


async def test_una_emision_sin_aggregate_version_no_manda_la_poliza_a_la_dlq(
    manejar: Any, coberturas: Any, recibido: Any, tx: Any, datos: dict[str, Any]
) -> None:
    """`versionado.leer_version` lanzaría EventoInvalido: `polizas` no publica la versión."""
    sobre = _sobre(datos)
    assert "aggregateVersion" not in sobre
    await manejar(recibido(sobre), tx)
    assert len(coberturas.filas) == 1


# --- Lo que va a la DLQ --------------------------------------------------------------------------
@pytest.mark.parametrize("version", [2, 0, "1", None])
async def test_una_version_distinta_de_1_va_a_la_dlq(
    manejar: Any, recibido: Any, tx: Any, datos: dict[str, Any], version: Any
) -> None:
    sobre = _sobre(datos)
    sobre["version"] = version
    with pytest.raises(EventoInvalido):
        await manejar(recibido(sobre), tx)


@pytest.mark.parametrize("version", [0, -1, "2", True, 1.5, []])
async def test_un_aggregate_version_que_no_es_un_entero_positivo_va_a_la_dlq(
    manejar: Any, recibido: Any, tx: Any, datos: dict[str, Any], version: Any
) -> None:
    sobre = _sobre(datos)
    sobre["aggregateVersion"] = version
    with pytest.raises(EventoInvalido):
        await manejar(recibido(sobre), tx)


@pytest.mark.parametrize(
    ("cambio", "campo"),
    [
        (lambda p: p.pop("polizaId"), "payload.polizaId"),
        (lambda p: p.update(numero="VH-2026-1"), "payload.numero"),
        (lambda p: p.update(vigencia={"desde": "2026-09-01"}), "payload.vigencia.hasta"),
        (lambda p: p["coberturaParametrica"].pop("umbral"), "coberturaParametrica.umbral"),
        (lambda p: p["coberturaParametrica"].update(umbral="mucho"), "coberturaParametrica.umbral"),
        (lambda p: p["coberturaParametrica"].update(tipoEvento="GRANIZO"), "tipoEvento"),
        (lambda p: p["coberturaParametrica"].update(valorPago="1400000.001"), "valorPago"),
    ],
)
async def test_un_cuerpo_que_no_valida_va_a_la_dlq_y_el_aviso_nombra_el_campo_sin_su_valor(
    manejar: Any,
    recibido: Any,
    tx: Any,
    datos: dict[str, Any],
    cambio: Any,
    campo: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sobre = _sobre(datos)
    cambio(sobre["payload"])
    with caplog.at_level(logging.WARNING), pytest.raises(EventoInvalido):
        await manejar(recibido(sobre), tx)
    aviso = next(r for r in caplog.records if "inválido" in r.getMessage())
    assert any(campo.split(".")[-1] in f for f in aviso.fields)  # type: ignore[attr-defined]
    assert "mucho" not in caplog.text and "VH-2026" not in caplog.text


@pytest.mark.parametrize(
    "cambios",
    [
        {"tipoEvento": "RETRASO_VUELO"},  # lluvia en minutos: unidad mm incoherente
        {"unidad": "min"},
        {"umbral": "0"},
        {"valorPago": "0.00"},
    ],
)
async def test_una_regla_incoherente_va_a_la_dlq(
    manejar: Any, coberturas: Any, recibido: Any, tx: Any, datos: dict[str, Any], cambios: Any
) -> None:
    sobre = _sobre(datos)
    sobre["payload"]["coberturaParametrica"].update(cambios)
    with pytest.raises(EventoInvalido):
        await manejar(recibido(sobre), tx)
    assert coberturas.filas == {}


async def test_una_vigencia_que_termina_antes_de_empezar_va_a_la_dlq(
    manejar: Any, recibido: Any, tx: Any, datos: dict[str, Any]
) -> None:
    sobre = _sobre(datos)
    sobre["payload"]["vigencia"] = {"desde": "2026-09-01", "hasta": "2026-08-31"}
    with pytest.raises(EventoInvalido):
        await manejar(recibido(sobre), tx)


async def test_una_cancelacion_sin_poliza_va_a_la_dlq(
    manejar: Any, recibido: Any, tx: Any, datos: dict[str, Any]
) -> None:
    sobre = _cancelacion(datos, "CAN-ORD-1")
    sobre["payload"] = {}
    with pytest.raises(EventoInvalido):
        await manejar(recibido(sobre), tx)


async def test_los_logs_no_traen_el_cuerpo_ni_el_numero_de_la_poliza(
    manejar: Any, recibido: Any, tx: Any, datos: dict[str, Any], telemetria: Any
) -> None:
    sobre = _sobre(datos)
    await manejar(recibido(sobre), tx)
    texto = telemetria.texto_logs()
    assert sobre["payload"]["numero"] not in texto
    assert json.dumps(sobre["payload"]["coberturaParametrica"]) not in texto
