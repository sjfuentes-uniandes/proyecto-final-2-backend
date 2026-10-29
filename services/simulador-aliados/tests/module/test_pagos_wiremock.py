"""Pasarela de pagos simulada (HU-W16): cobro y consulta de estado por pagador sintético.

El pagador es el `clienteId` sintético de contracts/datos-sinteticos/clientes-perfilamiento.json;
la `referencia` es la `claveRecaudo` (uuid) y vuelve en la respuesta. Los mapeos por `referencia`
fija (`pago-sintetico-*`) son anteriores y no cambian.
"""

import json
import time
from typing import Any
from uuid import uuid4

import httpx
import pytest

COBROS = "/pagos/v1/cobros"
ENCABEZADOS = {"X-Api-Key": "clave-sintetica", "Idempotency-Key": "idem-sintetica"}

APROBADO = "00000000-0000-4000-8000-000000000001"  # C01
RECHAZADO = "00000000-0000-4000-8000-000000000002"  # C02
MALFORMADO = "00000000-0000-4000-8000-000000000005"  # C05
LENTO = "00000000-0000-4000-8000-000000000006"  # C06
CAIDO = "00000000-0000-4000-8000-000000000007"  # C07
DESCONOCIDO = "00000000-0000-4000-8000-000000000003"  # C03: sin caso de pagos

NO_RECONOCIDA = {"error": "referencia_no_reconocida"}
CORTE_ADAPTADOR_S = 5.0
RAPIDO_S = 2.0


def _cuerpo(pagador: str, referencia: str | None = None, **cambios: Any) -> dict[str, Any]:
    cuerpo = {
        "referencia": referencia or str(uuid4()),
        "monto": "64638",
        "moneda": "COP",
        "pagador": pagador,
    }
    cuerpo.update(cambios)
    return {k: v for k, v in cuerpo.items() if v is not None}


def _cobrar(
    simulador: httpx.Client, cuerpo: dict[str, Any], encabezados: dict[str, str] | None = None
) -> tuple[httpx.Response, float]:
    inicio = time.monotonic()
    respuesta = simulador.post(
        COBROS, json=cuerpo, headers=ENCABEZADOS if encabezados is None else encabezados
    )
    return respuesta, time.monotonic() - inicio


def _consultar(
    simulador: httpx.Client, params: dict[str, str], encabezados: dict[str, str] | None = None
) -> tuple[httpx.Response, float]:
    inicio = time.monotonic()
    respuesta = simulador.get(
        COBROS,
        params=params,
        headers={"X-Api-Key": "clave-sintetica"} if encabezados is None else encabezados,
    )
    return respuesta, time.monotonic() - inicio


# --- Cobro por pagador -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pagador", "esperado"),
    [
        (APROBADO, {"estado": "APROBADO", "transaccionId": "TX-SINT-C01", "motivo": None}),
        (
            RECHAZADO,
            {"estado": "RECHAZADO", "transaccionId": None, "motivo": "FONDOS_INSUFICIENTES"},
        ),
    ],
    ids=["aprobado", "rechazado"],
)
def test_cobro_definitivo_devuelve_la_referencia(
    simulador: httpx.Client, pagador: str, esperado: dict[str, Any]
) -> None:
    referencia = str(uuid4())
    respuesta, duracion = _cobrar(simulador, _cuerpo(pagador, referencia))
    assert respuesta.status_code == 200
    assert respuesta.headers["Content-Type"].startswith("application/json")
    assert respuesta.json() == {"referencia": referencia, **esperado}
    assert duracion < RAPIDO_S


def test_cobro_lento_aprueba_despues_del_corte_del_adaptador(simulador: httpx.Client) -> None:
    referencia = str(uuid4())
    respuesta, duracion = _cobrar(simulador, _cuerpo(LENTO, referencia))
    assert respuesta.status_code == 200
    assert respuesta.json() == {
        "referencia": referencia,
        "estado": "APROBADO",
        "transaccionId": "TX-SINT-C06",
        "motivo": None,
    }
    assert duracion >= 7.5 > CORTE_ADAPTADOR_S


def test_cobro_caido_responde_503(simulador: httpx.Client) -> None:
    respuesta, duracion = _cobrar(simulador, _cuerpo(CAIDO))
    assert respuesta.status_code == 503
    assert respuesta.json() == {"error": "unavailable"}
    assert duracion < RAPIDO_S


def test_cobro_malformado_responde_json_invalido(simulador: httpx.Client) -> None:
    respuesta, _ = _cobrar(simulador, _cuerpo(MALFORMADO))
    assert respuesta.status_code == 200
    assert respuesta.headers["Content-Type"].startswith("application/json")
    assert respuesta.text == '{"estado": '
    with pytest.raises(json.JSONDecodeError):
        respuesta.json()


def test_cobro_de_un_pagador_sin_caso_cae_en_el_comodin(simulador: httpx.Client) -> None:
    respuesta, _ = _cobrar(simulador, _cuerpo(DESCONOCIDO))
    assert (respuesta.status_code, respuesta.json()) == (404, NO_RECONOCIDA)


@pytest.mark.parametrize("falta", ["X-Api-Key", "Idempotency-Key"])
def test_cobro_sin_encabezado_obligatorio_cae_en_el_comodin(
    simulador: httpx.Client, falta: str
) -> None:
    encabezados = {k: v for k, v in ENCABEZADOS.items() if k != falta}
    respuesta, _ = _cobrar(simulador, _cuerpo(APROBADO), encabezados)
    assert (respuesta.status_code, respuesta.json()) == (404, NO_RECONOCIDA)


@pytest.mark.parametrize(
    "cambios",
    [{"referencia": "no-es-uuid"}, {"monto": None}, {"moneda": None}],
    ids=["referencia-no-uuid", "sin-monto", "sin-moneda"],
)
def test_cobro_con_cuerpo_incompleto_cae_en_el_comodin(
    simulador: httpx.Client, cambios: dict[str, Any]
) -> None:
    cuerpo = _cuerpo(APROBADO)
    cuerpo.update(cambios)
    cuerpo = {k: v for k, v in cuerpo.items() if v is not None}
    respuesta, _ = _cobrar(simulador, cuerpo)
    assert (respuesta.status_code, respuesta.json()) == (404, NO_RECONOCIDA)


# --- Consulta de estado (conciliación, P16-3) ------------------------------------------------


@pytest.mark.parametrize(
    ("pagador", "esperado"),
    [
        (APROBADO, {"estado": "APROBADO", "transaccionId": "TX-SINT-C01", "motivo": None}),
        (
            RECHAZADO,
            {"estado": "RECHAZADO", "transaccionId": None, "motivo": "FONDOS_INSUFICIENTES"},
        ),
        (LENTO, {"estado": "APROBADO", "transaccionId": "TX-SINT-C06", "motivo": None}),
    ],
    ids=["aprobado", "rechazado", "lento-si-cobro"],
)
def test_consulta_devuelve_el_estado_definitivo(
    simulador: httpx.Client, pagador: str, esperado: dict[str, Any]
) -> None:
    referencia = str(uuid4())
    respuesta, duracion = _consultar(simulador, {"referencia": referencia, "pagador": pagador})
    assert respuesta.status_code == 200
    assert respuesta.headers["Content-Type"].startswith("application/json")
    assert respuesta.json() == {"referencia": referencia, **esperado}
    assert duracion < RAPIDO_S


def test_consulta_del_caido_responde_que_el_cobro_no_existe(simulador: httpx.Client) -> None:
    respuesta, duracion = _consultar(simulador, {"referencia": str(uuid4()), "pagador": CAIDO})
    assert (respuesta.status_code, respuesta.json()) == (404, {"error": "cobro_no_encontrado"})
    assert duracion < RAPIDO_S


@pytest.mark.parametrize(
    ("params", "encabezados"),
    [
        ({"referencia": str(uuid4()), "pagador": MALFORMADO}, None),
        ({"referencia": str(uuid4()), "pagador": DESCONOCIDO}, None),
        ({"pagador": APROBADO}, None),
        ({"referencia": str(uuid4()), "pagador": APROBADO}, {}),
    ],
    ids=["malformado", "pagador-sin-caso", "sin-referencia", "sin-api-key"],
)
def test_consulta_sin_caso_cae_en_el_comodin(
    simulador: httpx.Client, params: dict[str, str], encabezados: dict[str, str] | None
) -> None:
    respuesta, _ = _consultar(simulador, params, encabezados)
    assert (respuesta.status_code, respuesta.json()) == (404, NO_RECONOCIDA)


# --- Los mapeos anteriores (por `referencia` fija) siguen igual ------------------------------


@pytest.mark.parametrize(
    ("referencia", "estado", "cuerpo"),
    [
        (
            "pago-sintetico-aprobado",
            200,
            {"estado": "APROBADO", "transaccionId": "TX-SINT-0001", "motivo": None},
        ),
        ("pago-sintetico-caido", 503, {"error": "unavailable"}),
        ("pago-sintetico-no-existe", 404, NO_RECONOCIDA),
    ],
    ids=["aprobado", "caido", "comodin"],
)
def test_mapeos_anteriores_responden_igual(
    simulador: httpx.Client, referencia: str, estado: int, cuerpo: dict[str, Any]
) -> None:
    respuesta, duracion = _cobrar(simulador, {"referencia": referencia})
    assert (respuesta.status_code, respuesta.json()) == (estado, cuerpo)
    assert duracion < RAPIDO_S


def test_mapeo_anterior_malformado_responde_igual(simulador: httpx.Client) -> None:
    respuesta, _ = _cobrar(simulador, {"referencia": "pago-sintetico-malformado"})
    assert (respuesta.status_code, respuesta.text) == (200, '{"estado": ')


def test_mapeo_anterior_lento_responde_igual(simulador: httpx.Client) -> None:
    respuesta, duracion = _cobrar(simulador, {"referencia": "pago-sintetico-lento"})
    assert respuesta.status_code == 200
    assert respuesta.json() == {
        "estado": "APROBADO",
        "transaccionId": "TX-SINT-0001",
        "motivo": None,
    }
    assert duracion >= 7.5


def test_una_referencia_fija_gana_aunque_traiga_un_pagador(simulador: httpx.Client) -> None:
    """Los mapeos nuevos exigen una referencia uuid: no se solapan con los anteriores."""
    respuesta, _ = _cobrar(simulador, _cuerpo(CAIDO, "pago-sintetico-aprobado"))
    assert respuesta.status_code == 200
    assert respuesta.json()["transaccionId"] == "TX-SINT-0001"
