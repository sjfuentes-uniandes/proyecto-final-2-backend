"""Pasarela de pagos simulada (HU-W23): desembolso de la indemnización y consulta de estado por
beneficiario sintético.

El `beneficiarioRef` sintético de `SiniestroAprobado` llega al aliado como `beneficiario` y elige
el escenario; la `referencia` es `IND-<codigo>-NN` y vuelve en la respuesta. Las referencias de la
semilla de R7 (`ben-sint-001` a `ben-sint-008`) caen en confirmado, salvo la última, que rechaza
(AC4 y R7-01). Desde HU-W22, el titular de un siniestro paramétrico (`ben-par-<clienteId>`) también
confirma, para que R5 llegue al pago. Cualquier otro beneficiario cae en el respaldo de prioridad 10
(404).
"""

import json
import re
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

DESEMBOLSOS = "/pagos/v1/desembolsos"
ENCABEZADOS = {"X-Api-Key": "clave-sintetica", "Idempotency-Key": "idem-sintetica"}
REFERENCIA = "IND-88-2026-0442-01"
SEMILLA = (
    Path(__file__).resolve().parents[4]
    / "services/siniestros/src/siniestros/semillas/siniestros_r7.json"
)
NO_RECONOCIDA = {"error": "referencia_no_reconocida"}
CORTE_ADAPTADOR_S = 5.0
RAPIDO_S = 2.0


def _cuerpo(beneficiario: str, referencia: str = REFERENCIA, **cambios: Any) -> dict[str, Any]:
    cuerpo = {
        "referencia": referencia,
        "monto": "2500000.00",
        "moneda": "COP",
        "beneficiario": beneficiario,
    }
    cuerpo.update(cambios)
    return {k: v for k, v in cuerpo.items() if v is not None}


def _desembolsar(
    simulador: httpx.Client, cuerpo: dict[str, Any], encabezados: dict[str, str] | None = None
) -> tuple[httpx.Response, float]:
    inicio = time.monotonic()
    respuesta = simulador.post(
        DESEMBOLSOS, json=cuerpo, headers=ENCABEZADOS if encabezados is None else encabezados
    )
    return respuesta, time.monotonic() - inicio


def _consultar(
    simulador: httpx.Client, beneficiario: str, referencia: str = REFERENCIA
) -> httpx.Response:
    return simulador.get(
        DESEMBOLSOS,
        params={"referencia": referencia, "beneficiario": beneficiario},
        headers={"X-Api-Key": "clave-sintetica"},
    )


def _confirmado(referencia: str = REFERENCIA, transaccion: str | None = None) -> dict[str, Any]:
    return {
        "referencia": referencia,
        "estado": "CONFIRMADO",
        "transaccionId": transaccion or f"TRX-SINT-{referencia}",
        "motivo": None,
    }


RECHAZADO = {
    "referencia": REFERENCIA,
    "estado": "RECHAZADO",
    "transaccionId": None,
    "motivo": "BENEFICIARIO_NO_HABILITADO",
}


@pytest.mark.parametrize("numero", range(1, 8))
def test_los_beneficiarios_de_la_semilla_confirman(simulador: httpx.Client, numero: int) -> None:
    beneficiario = f"ben-sint-{numero:03d}"
    respuesta, duracion = _desembolsar(simulador, _cuerpo(beneficiario))
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == _confirmado()
    assert duracion < RAPIDO_S
    consulta = _consultar(simulador, beneficiario)
    assert consulta.status_code == 200, consulta.text
    assert consulta.json() == _confirmado()


def test_la_semilla_de_r7_cae_en_confirmado_salvo_un_caso_que_rechaza(
    simulador: httpx.Client,
) -> None:
    """Todos los `beneficiarioRef` de la semilla tienen escenario; solo VEN-01 (008) rechaza."""
    texto = SEMILLA.read_text(encoding="utf-8")
    referencias = sorted(set(re.findall(r'"beneficiarioRef":\s*"([^"]+)"', texto)))
    assert referencias == [f"ben-sint-{n:03d}" for n in range(1, 9)]
    estados = {r: _desembolsar(simulador, _cuerpo(r))[0].json()["estado"] for r in referencias}
    assert sorted(set(estados.values())) == ["CONFIRMADO", "RECHAZADO"]
    assert [r for r, e in estados.items() if e == "RECHAZADO"] == ["ben-sint-008"]
    assert json.loads(texto)  # la semilla es JSON válido


@pytest.mark.parametrize(
    "beneficiario",
    [
        "ben-par-00000000-0000-4000-8000-000000000002",
        "ben-par-3f2a9c1e-7b64-4d0a-9e51-0c8d2f6a1b77",
    ],
)
def test_el_titular_de_un_siniestro_parametrico_confirma(
    simulador: httpx.Client, beneficiario: str
) -> None:
    """HU-W22: `ben-par-<clienteId>` es el beneficiario de un paramétrico (R-178)."""
    referencia = "IND-88-2026-1001-01"
    respuesta, duracion = _desembolsar(simulador, _cuerpo(beneficiario, referencia))
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == _confirmado(referencia)
    assert duracion < RAPIDO_S
    consulta = _consultar(simulador, beneficiario, referencia)
    assert consulta.status_code == 200, consulta.text
    assert consulta.json() == _confirmado(referencia)


@pytest.mark.parametrize(
    "beneficiario",
    ["ben-par-", "ben-par-no-es-un-uuid", "ben-par-00000000-0000-4000-8000-00000000000G"],
)
def test_un_beneficiario_paramétrico_sin_cliente_valido_cae_en_el_respaldo(
    simulador: httpx.Client, beneficiario: str
) -> None:
    respuesta, _ = _desembolsar(simulador, _cuerpo(beneficiario))
    assert (respuesta.status_code, respuesta.json()) == (404, NO_RECONOCIDA)
    assert _consultar(simulador, beneficiario).json() == NO_RECONOCIDA


@pytest.mark.parametrize("beneficiario", ["ben-sint-008", "ben-sint-rechaza"])
def test_rechazo_con_motivo(simulador: httpx.Client, beneficiario: str) -> None:
    respuesta, _ = _desembolsar(simulador, _cuerpo(beneficiario))
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == RECHAZADO
    consulta = _consultar(simulador, beneficiario)
    assert consulta.status_code == 200
    assert consulta.json() == RECHAZADO


def test_lento_supera_el_timeout_del_adaptador_y_el_desembolso_si_ocurre(
    simulador: httpx.Client,
) -> None:
    respuesta, duracion = _desembolsar(simulador, _cuerpo("ben-sint-lento"))
    assert respuesta.status_code == 200
    assert duracion > CORTE_ADAPTADOR_S
    consulta = _consultar(simulador, "ben-sint-lento")
    assert consulta.status_code == 200
    assert consulta.json() == _confirmado()


def test_caido_responde_503_y_la_consulta_dice_que_la_orden_nunca_llego(
    simulador: httpx.Client,
) -> None:
    respuesta, _ = _desembolsar(simulador, _cuerpo("ben-sint-caido"))
    assert respuesta.status_code == 503
    assert respuesta.json() == {"error": "unavailable"}
    consulta = _consultar(simulador, "ben-sint-caido")
    assert consulta.status_code == 404
    assert consulta.json() == {"error": "desembolso_no_encontrado"}


def test_duplicado_devuelve_siempre_el_mismo_transaccion_id(simulador: httpx.Client) -> None:
    esperado = _confirmado(transaccion="TRX-SINT-DUPLICADO")
    for _ in range(3):
        respuesta, _ = _desembolsar(simulador, _cuerpo("ben-sint-duplicado"))
        assert respuesta.status_code == 200
        assert respuesta.json() == esperado
    assert _consultar(simulador, "ben-sint-duplicado").json() == esperado


def test_la_referencia_vuelve_en_la_respuesta(simulador: httpx.Client) -> None:
    referencia = "IND-88-2026-1234-01"
    respuesta, _ = _desembolsar(simulador, _cuerpo("ben-sint-002", referencia))
    assert respuesta.json()["referencia"] == referencia
    assert respuesta.json()["transaccionId"] == f"TRX-SINT-{referencia}"


@pytest.mark.parametrize(
    ("cuerpo", "encabezados"),
    [
        (_cuerpo("ben-sint-999"), None),
        (_cuerpo("ben-sint-009"), None),
        (_cuerpo("ben-sint-001", "no-es-una-referencia"), None),
        (_cuerpo("ben-sint-001", monto=None), None),
        (_cuerpo("ben-sint-001", moneda=None), None),
        (_cuerpo("ben-sint-001"), {"Idempotency-Key": "idem"}),
        (_cuerpo("ben-sint-001"), {"X-Api-Key": "clave"}),
    ],
)
def test_otro_beneficiario_o_solicitud_incompleta_cae_en_el_respaldo(
    simulador: httpx.Client, cuerpo: dict[str, Any], encabezados: dict[str, str] | None
) -> None:
    respuesta, _ = _desembolsar(simulador, cuerpo, encabezados)
    assert respuesta.status_code == 404
    assert respuesta.json() == NO_RECONOCIDA


def test_consulta_de_otro_beneficiario_o_sin_clave_cae_en_el_respaldo(
    simulador: httpx.Client,
) -> None:
    assert _consultar(simulador, "ben-sint-999").json() == NO_RECONOCIDA
    sin_clave = simulador.get(
        DESEMBOLSOS, params={"referencia": REFERENCIA, "beneficiario": "ben-sint-001"}
    )
    assert sin_clave.status_code == 404
    assert sin_clave.json() == NO_RECONOCIDA
