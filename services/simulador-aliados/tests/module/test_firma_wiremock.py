"""Firma electrónica simulada (HU-W17): firma de la huella del documento por cliente sintético.

El `firmante` es el `clienteId` sintético de contracts/datos-sinteticos/clientes-perfilamiento.json;
el `documentoId` (uuid, = `polizaId`) y la `huella` (`sha256:` + 64 hex) vuelven en la respuesta.
Los mapeos por `documentoId` fijo (`doc-sintetico-*`) son anteriores y no cambian.
"""

import hashlib
import json
import time
from typing import Any
from uuid import uuid4

import httpx
import pytest

FIRMAS = "/firma/v1/firmas"
ENCABEZADOS = {"X-Api-Key": "clave-sintetica", "Idempotency-Key": "idem-sintetica"}

FIRMADO = "00000000-0000-4000-8000-000000000001"  # C01
MALFORMADO = "00000000-0000-4000-8000-000000000005"  # C05
LENTO = "00000000-0000-4000-8000-000000000006"  # C06
CAIDO = "00000000-0000-4000-8000-000000000007"  # C07
DESCONOCIDO = "00000000-0000-4000-8000-000000000003"  # C03: sin caso de firma

SELLO = "2026-01-01T00:00:00Z"
NO_RECONOCIDO = {"error": "documento_no_reconocido"}
CORTE_ADAPTADOR_S = 5.0
RAPIDO_S = 2.0


def _huella() -> str:
    return "sha256:" + hashlib.sha256(uuid4().bytes).hexdigest()


def _cuerpo(firmante: str, documento_id: str | None = None, **cambios: Any) -> dict[str, Any]:
    cuerpo = {
        "documentoId": documento_id or str(uuid4()),
        "huella": _huella(),
        "firmante": firmante,
    }
    cuerpo.update(cambios)
    return {k: v for k, v in cuerpo.items() if v is not None}


def _firmar(
    simulador: httpx.Client, cuerpo: dict[str, Any], encabezados: dict[str, str] | None = None
) -> tuple[httpx.Response, float]:
    inicio = time.monotonic()
    respuesta = simulador.post(
        FIRMAS, json=cuerpo, headers=ENCABEZADOS if encabezados is None else encabezados
    )
    return respuesta, time.monotonic() - inicio


# --- Firma por cliente ------------------------------------------------------------------------


def test_firma_devuelve_el_documento_y_la_huella_recibidos(simulador: httpx.Client) -> None:
    cuerpo = _cuerpo(FIRMADO)
    respuesta, duracion = _firmar(simulador, cuerpo)
    assert respuesta.status_code == 200
    assert respuesta.headers["Content-Type"].startswith("application/json")
    assert respuesta.json() == {
        "documentoId": cuerpo["documentoId"],
        "estado": "FIRMADO",
        "firmaId": "FIR-SINT-C01",
        "selloTiempo": SELLO,
        "huella": cuerpo["huella"],
    }
    assert duracion < RAPIDO_S


def test_firma_lenta_responde_despues_del_corte_del_adaptador(simulador: httpx.Client) -> None:
    cuerpo = _cuerpo(LENTO)
    respuesta, duracion = _firmar(simulador, cuerpo)
    assert respuesta.status_code == 200
    assert respuesta.json() == {
        "documentoId": cuerpo["documentoId"],
        "estado": "FIRMADO",
        "firmaId": "FIR-SINT-C06",
        "selloTiempo": SELLO,
        "huella": cuerpo["huella"],
    }
    assert duracion >= 7.5 > CORTE_ADAPTADOR_S


def test_firma_caida_responde_503(simulador: httpx.Client) -> None:
    respuesta, duracion = _firmar(simulador, _cuerpo(CAIDO))
    assert respuesta.status_code == 503
    assert respuesta.json() == {"error": "unavailable"}
    assert duracion < RAPIDO_S


def test_firma_malformada_responde_json_invalido(simulador: httpx.Client) -> None:
    respuesta, duracion = _firmar(simulador, _cuerpo(MALFORMADO))
    assert respuesta.status_code == 200
    assert respuesta.headers["Content-Type"].startswith("application/json")
    assert respuesta.text == '{"estado": '
    with pytest.raises(json.JSONDecodeError):
        respuesta.json()
    assert duracion < RAPIDO_S


def test_firma_de_un_cliente_sin_caso_cae_en_el_comodin(simulador: httpx.Client) -> None:
    respuesta, _ = _firmar(simulador, _cuerpo(DESCONOCIDO))
    assert (respuesta.status_code, respuesta.json()) == (404, NO_RECONOCIDO)


@pytest.mark.parametrize("falta", ["X-Api-Key", "Idempotency-Key"])
def test_firma_sin_encabezado_obligatorio_cae_en_el_comodin(
    simulador: httpx.Client, falta: str
) -> None:
    encabezados = {k: v for k, v in ENCABEZADOS.items() if k != falta}
    respuesta, _ = _firmar(simulador, _cuerpo(FIRMADO), encabezados)
    assert (respuesta.status_code, respuesta.json()) == (404, NO_RECONOCIDO)


@pytest.mark.parametrize(
    "cambios",
    [
        {"documentoId": "no-es-uuid"},
        {"documentoId": None},
        {"huella": "a" * 64},
        {"huella": "md5:" + "a" * 32},
        {"huella": "sha256:" + "A" * 64},
        {"huella": "sha256:" + "a" * 63},
        {"huella": "sha256:" + "a" * 65},
        {"huella": "x-sha256:" + "a" * 64},
        {"huella": None},
        {"firmante": None},
    ],
    ids=[
        "documento-no-uuid",
        "sin-documento",
        "huella-sin-prefijo",
        "huella-md5",
        "huella-mayusculas",
        "huella-corta",
        "huella-larga",
        "huella-con-prefijo-de-mas",
        "sin-huella",
        "sin-firmante",
    ],
)
def test_firma_con_cuerpo_invalido_cae_en_el_comodin(
    simulador: httpx.Client, cambios: dict[str, Any]
) -> None:
    cuerpo = _cuerpo(FIRMADO)
    cuerpo.update(cambios)
    cuerpo = {k: v for k, v in cuerpo.items() if v is not None}
    respuesta, _ = _firmar(simulador, cuerpo)
    assert (respuesta.status_code, respuesta.json()) == (404, NO_RECONOCIDO)


# --- Los mapeos anteriores (por `documentoId` fijo) siguen igual ------------------------------

ANTERIOR_FIRMADO = {"estado": "FIRMADO", "firmaId": "FIR-SINT-0001", "selloTiempo": SELLO}


@pytest.mark.parametrize(
    ("documento_id", "estado", "cuerpo"),
    [
        ("doc-sintetico-firmado", 200, ANTERIOR_FIRMADO),
        ("doc-sintetico-caido", 503, {"error": "unavailable"}),
        ("doc-sintetico-no-existe", 404, NO_RECONOCIDO),
    ],
    ids=["firmado", "caido", "comodin"],
)
def test_mapeos_anteriores_responden_igual(
    simulador: httpx.Client, documento_id: str, estado: int, cuerpo: dict[str, Any]
) -> None:
    respuesta, duracion = _firmar(
        simulador, {"documentoId": documento_id}, {"X-Api-Key": "clave-sintetica"}
    )
    assert (respuesta.status_code, respuesta.json()) == (estado, cuerpo)
    assert duracion < RAPIDO_S


def test_mapeo_anterior_malformado_responde_igual(simulador: httpx.Client) -> None:
    respuesta, _ = _firmar(
        simulador, {"documentoId": "doc-sintetico-malformado"}, {"X-Api-Key": "clave-sintetica"}
    )
    assert (respuesta.status_code, respuesta.text) == (200, '{"estado": ')


def test_mapeo_anterior_lento_responde_igual(simulador: httpx.Client) -> None:
    respuesta, duracion = _firmar(
        simulador, {"documentoId": "doc-sintetico-lento"}, {"X-Api-Key": "clave-sintetica"}
    )
    assert (respuesta.status_code, respuesta.json()) == (200, ANTERIOR_FIRMADO)
    assert duracion >= 7.5


def test_mapeo_anterior_sin_api_key_cae_en_el_comodin(simulador: httpx.Client) -> None:
    respuesta, _ = _firmar(simulador, {"documentoId": "doc-sintetico-firmado"}, {})
    assert (respuesta.status_code, respuesta.json()) == (404, NO_RECONOCIDO)


def test_un_documento_fijo_gana_aunque_traiga_un_firmante(simulador: httpx.Client) -> None:
    """Los mapeos nuevos exigen un documentoId uuid: no se solapan con los anteriores."""
    respuesta, _ = _firmar(simulador, _cuerpo(CAIDO, "doc-sintetico-firmado"))
    assert (respuesta.status_code, respuesta.json()) == (200, ANTERIOR_FIRMADO)
