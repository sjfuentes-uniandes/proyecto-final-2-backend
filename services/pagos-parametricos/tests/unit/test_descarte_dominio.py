"""HU-W24 (N1): causa, póliza referida y corrección de un mensaje de la cola de descarte."""

import json
from typing import Any

import pytest
from pagos_parametricos.domain.descarte import (
    Causa,
    CorreccionInvalida,
    FalloAnotado,
    aplicar_correccion,
    causa_de,
    interpretar,
    poliza_de,
    resumen_por_causa,
)

EVENTO = "4b0c9a35-55a4-4a7e-9f1e-0d6c8c6b2a11"


def _cuerpo(**payload: Any) -> str:
    return json.dumps(
        {
            "eventId": EVENTO,
            "aggregateId": EVENTO,
            "type": "EventoParametricoObservado",
            "version": 1,
            "occurredAt": "2026-09-13T14:06:12Z",
            "correlationId": "corr-w24-01",
            "payload": {
                "evento": "lluvia_acumulada",
                "numeroPoliza": "500-2026-000411",
                "estacion": "EST-CUND-0114",
                "precipitacionMm": "142,7",
                "ventanaHoras": 24,
            }
            | payload,
        }
    )


@pytest.mark.parametrize(
    ("fallo", "causa"),
    [
        (None, Causa.DESCONOCIDA),
        (FalloAnotado("envenenado", "esquema", 5), Causa.FORMATO_INVALIDO),
        (FalloAnotado("envenenado", "json_invalido", 5), Causa.FORMATO_INVALIDO),
        (FalloAnotado("fallo_transaccion", "PolizaInexistente", 5), Causa.REFERENCIA_INEXISTENTE),
        (FalloAnotado("fallo_transaccion", "OperationalError", 5), Causa.ERROR_PROCESAMIENTO),
    ],
)
def test_la_causa_sale_del_desenlace_que_anoto_el_consumidor(
    fallo: FalloAnotado | None, causa: Causa
) -> None:
    assert causa_de(fallo) is causa


def test_interpreta_evento_poliza_e_intentos() -> None:
    mensaje = interpretar(
        "m-1",
        _cuerpo(),
        recibido_en="2026-09-13T14:06:12+00:00",
        recepciones=6,
        fallo=FalloAnotado("envenenado", "esquema", 5),
    )

    assert (mensaje.evento_id, mensaje.tipo, mensaje.poliza) == (
        EVENTO,
        "EventoParametricoObservado",
        "500-2026-000411",
    )
    # Las recepciones que vio el consumidor, no las de la DLQ (que siguen contando al leerla).
    assert mensaje.intentos == 5
    assert (mensaje.causa, mensaje.detalle) == (Causa.FORMATO_INVALIDO, "esquema")


def test_un_cuerpo_que_no_es_sobre_se_interpreta_sin_datos() -> None:
    mensaje = interpretar("m-1", "{no es json", recibido_en=None, recepciones=5, fallo=None)

    assert (mensaje.evento_id, mensaje.poliza, mensaje.intentos) == (None, None, 5)
    assert mensaje.causa is Causa.DESCONOCIDA


@pytest.mark.parametrize("payload", [{"numeroPoliza": 7}, {"numeroPoliza": "x" * 65}, {}])
def test_la_poliza_solo_si_es_una_referencia(payload: dict[str, Any]) -> None:
    sobre = json.loads(_cuerpo()) | {"payload": payload}

    assert poliza_de(sobre) is None


def test_resume_por_causa() -> None:
    mensajes = [
        interpretar(
            "a",
            _cuerpo(),
            recibido_en=None,
            recepciones=5,
            fallo=FalloAnotado("envenenado", None, 5),
        ),
        interpretar(
            "b",
            _cuerpo(),
            recibido_en=None,
            recepciones=5,
            fallo=FalloAnotado("envenenado", None, 5),
        ),
        interpretar("c", _cuerpo(), recibido_en=None, recepciones=5, fallo=None),
    ]

    assert resumen_por_causa(mensajes) == {Causa.FORMATO_INVALIDO: 2, Causa.DESCONOCIDA: 1}


def test_la_correccion_cambia_solo_el_campo_y_conserva_el_event_id() -> None:
    original = _cuerpo()

    corregido = json.loads(aplicar_correccion(original, {"precipitacionMm": 142.7}))

    esperado = json.loads(original)
    esperado["payload"]["precipitacionMm"] = 142.7
    assert corregido == esperado
    assert corregido["eventId"] == EVENTO


@pytest.mark.parametrize(
    ("cuerpo", "cambios", "campos"),
    [
        ("{no es json", {"precipitacionMm": 1}, []),
        (json.dumps({"eventId": EVENTO}), {"precipitacionMm": 1}, []),
        (_cuerpo(), {}, []),
        (_cuerpo(), {"montoPago": 1}, ["montoPago"]),
        (_cuerpo(), {"precipitacionMm": {"valor": 1}}, ["precipitacionMm"]),
        (_cuerpo(), {"precipitacionMm": [1]}, ["precipitacionMm"]),
    ],
    ids=["no-json", "sin-payload", "sin-cambios", "campo-nuevo", "objeto", "lista"],
)
def test_una_correccion_invalida_dice_que_campos_fallan(
    cuerpo: str, cambios: dict[str, Any], campos: list[str]
) -> None:
    with pytest.raises(CorreccionInvalida) as error:
        aplicar_correccion(cuerpo, cambios)

    assert error.value.campos == campos
