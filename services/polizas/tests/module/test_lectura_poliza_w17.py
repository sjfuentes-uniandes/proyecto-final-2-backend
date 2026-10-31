"""MT-W17-09 (17.9): las rutas de la póliza contra PostgreSQL, LocalStack (S3 con
`S3_SKIP_SIGNATURE_VALIDATION=0`, SNS y SQS) y WireMock con las respuestas del contrato de
adaptador-firma. La póliza la lleva a ACTIVA la tarea emisora real, desde un `RecaudoConfirmado`
en `polizas-recaudos` (como la 17.8).

- ACTIVA: `GET …/poliza` trae su documento y su firma; la URL de `GET …/documento` descarga la
  versión registrada (aunque haya una más nueva del mismo objeto), cuyo sha256 es la huella; la
  verificación la valida con esa huella y no con otra.
- EMITIENDO con el documento subido y sin firma (firma caída): `GET …/documento` da 409 y la
  verificación no la valida, aunque la huella sea la del documento.
- Recaudo pendiente o rechazado: `GET …/poliza` da 404 POLIZA_NO_ENCONTRADA (AC2).
"""

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import boto3
import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

docker = pytest.importorskip("docker")
pytestmark = pytest.mark.anyio
REGION = "us-east-1"
ASESOR = {"X-Actor-Id": "sub-asesor-1", "X-Actor-Rol": "ASESOR"}
SUSCRIPTOR = {"X-Actor-Id": "sub-suscriptor-1", "X-Actor-Rol": "SUSCRIPTOR"}
TODAS = {
    "leidoCondiciones": True,
    "informacionVeraz": True,
    "debitoAutomatico": True,
    "avisosElectronicos": True,
}
RESUELTO_EN = "2026-10-22T15:00:01.250000Z"
VERIFICACIONES = "/v1/polizas/verificaciones"


# --- Ayudas ------------------------------------------------------------------------------------
def _filas(url: str, consulta: str, parametros: Any = None) -> list[dict[str, Any]]:
    with psycopg.connect(url, autocommit=True) as conexion:
        return conexion.cursor(row_factory=dict_row).execute(consulta, parametros).fetchall()


async def _esperar(condicion: Callable[[], Awaitable[bool] | bool], limite_s: float = 30) -> None:
    fin = time.monotonic() + limite_s
    while True:
        resultado = condicion()
        if await resultado if isinstance(resultado, Awaitable) else resultado:
            return
        assert time.monotonic() < fin, "la condición no se cumplió a tiempo"
        await asyncio.sleep(0.1)


def _sobre(tipo: str, clave: str) -> dict[str, Any]:
    recaudo_id = str(uuid4())
    datos: dict[str, Any] = {"recaudoId": recaudo_id, "claveRecaudo": clave}
    if tipo == "RecaudoConfirmado":
        datos |= {
            "cotizacionId": str(uuid4()),
            "monto": {"valor": "64638.10", "moneda": "COP"},
            "transaccionId": "TX-SINTETICA-0001",
            "confirmadoEn": RESUELTO_EN,
        }
    else:
        datos |= {
            "cotizacionId": str(uuid4()),
            "motivo": "FONDOS_INSUFICIENTES",
            "rechazadoEn": RESUELTO_EN,
        }
    return {
        "eventId": str(uuid4()),
        "aggregateId": recaudo_id,
        "type": tipo,
        "version": 1,
        "occurredAt": RESUELTO_EN,
        "correlationId": "corr-lectura-modulo",
        "payload": datos,
    }


async def _aceptar(api: Any, sinteticos: Any) -> dict[str, Any]:
    """Suscripción y aceptación de S01 por la API de polizas; devuelve la aceptación."""
    decision = await api.post(
        "/v1/suscripciones",
        json={"cotizacionId": sinteticos.cotizacion_id("S01")},
        headers={**ASESOR, "Idempotency-Key": str(uuid4())},
    )
    assert decision.status_code == 201, decision.json()
    cuerpo = decision.json()
    aceptacion = await api.post(
        f"/v1/cotizaciones/{cuerpo['cotizacionId']}/aceptacion",
        json={
            "decisionId": cuerpo["id"],
            "versionDeclaraciones": "2026.10.1",
            "declaraciones": TODAS,
        },
        headers={**ASESOR, "Idempotency-Key": str(uuid4())},
    )
    assert aceptacion.status_code == 201, aceptacion.json()
    resultado: dict[str, Any] = aceptacion.json()
    return resultado


def _sha256(contenido: bytes) -> str:
    return "sha256:" + hashlib.sha256(contenido).hexdigest()


def _poliza(url: str, aceptacion_id: str) -> dict[str, Any]:
    [fila] = _filas(url, "SELECT * FROM polizas WHERE aceptacion_id = %s", (aceptacion_id,))
    return fila


# --- ACTIVA ------------------------------------------------------------------------------------
async def test_activa_con_su_documento_descargable_y_verificable(
    abrir_api_emisora: Any,
    cola_recaudos: Any,
    sinteticos: Any,
    limpia: str,
    localstack: str,
    bucket: str,
) -> None:
    async with abrir_api_emisora() as api:
        aceptacion = await _aceptar(api, sinteticos)
        await asyncio.to_thread(
            cola_recaudos.publicar, _sobre("RecaudoConfirmado", aceptacion["id"])
        )
        await _esperar(lambda: _filas(limpia, "SELECT 1 FROM polizas WHERE estado = 'ACTIVA'"))
        fila = _poliza(limpia, aceptacion["id"])
        # Otra versión, más nueva, del mismo objeto: la URL debe seguir siendo la registrada.
        s3 = boto3.client("s3", region_name=REGION, endpoint_url=localstack)
        await asyncio.to_thread(
            s3.put_object,
            Bucket=bucket,
            Key=fila["documento_clave"],
            Body=b"%PDF-otra version",
            ServerSideEncryption="AES256",
        )

        respuesta = await api.get(
            f"/v1/cotizaciones/{aceptacion['cotizacionId']}/poliza", headers=ASESOR
        )
        enlace = await api.get(f"/v1/polizas/{fila['poliza_id']}/documento", headers=SUSCRIPTOR)
        assert respuesta.status_code == 200, respuesta.json()
        poliza = respuesta.json()
        huella = poliza["documento"]["huella"]
        valida = await api.post(
            VERIFICACIONES, json={"numero": poliza["numero"], "huella": huella}, headers=ASESOR
        )
        otra = await api.post(
            VERIFICACIONES,
            json={"numero": poliza["numero"], "huella": "sha256:" + "0" * 64},
            headers=ASESOR,
        )

    # La póliza de la ruta 1 es la de la base, ACTIVA, con su documento y su firma.
    assert poliza["polizaId"] == str(fila["poliza_id"])
    assert (poliza["estado"], poliza["numero"]) == ("ACTIVA", fila["numero"])
    assert poliza["documento"] == {
        "huella": fila["documento_huella"],
        "versionId": fila["documento_version_id"],
    }
    assert poliza["firma"]["firmaId"] == fila["firma_id"] == "FIR-SINT-C01"
    assert datetime.fromisoformat(poliza["emitidaEn"]) == fila["emitida_en"]
    assert datetime.fromisoformat(poliza["aceptadaEn"]) == datetime.fromisoformat(
        aceptacion["aceptadaEn"]
    )
    assert poliza["recaudadaEn"] == RESUELTO_EN
    assert (poliza["prima"], poliza["moneda"]) == ("64638", "COP")
    assert poliza["vigencia"] == {
        "desde": fila["vigencia_desde"].isoformat(),
        "hasta": fila["vigencia_hasta"].isoformat(),
    }

    # La URL de la ruta 2 descarga la versión registrada: su sha256 es la huella de la ruta 1.
    assert enlace.status_code == 200, enlace.json()
    consulta = parse_qs(urlsplit(enlace.json()["url"]).query)
    assert consulta["versionId"] == [fila["documento_version_id"]]
    async with httpx.AsyncClient(timeout=10) as cliente:
        descarga = await cliente.get(enlace.json()["url"])
    assert descarga.status_code == 200
    assert descarga.content.startswith(b"%PDF-") and descarga.content != b"%PDF-otra version"
    assert _sha256(descarga.content) == huella
    assert len(descarga.content) == fila["documento_tamano_bytes"]

    # La ruta 3 la valida con esa huella y no con otra, con la misma forma.
    assert valida.json() == {
        "valido": True,
        "numero": poliza["numero"],
        "firmaId": "FIR-SINT-C01",
        "selloTiempo": poliza["firma"]["selloTiempo"],
    }
    assert otra.json() == {
        "valido": False,
        "numero": poliza["numero"],
        "firmaId": None,
        "selloTiempo": None,
    }


# --- EMITIENDO ---------------------------------------------------------------------------------
async def test_emitiendo_con_el_documento_subido_da_409_y_no_se_valida(
    abrir_api_emisora: Any,
    cola_recaudos: Any,
    firma: Any,
    sinteticos: Any,
    limpia: str,
) -> None:
    # La firma caída deja la póliza EMITIENDO con el documento ya registrado.
    firma.modo("caido")
    async with abrir_api_emisora() as api:
        aceptacion = await _aceptar(api, sinteticos)
        await asyncio.to_thread(
            cola_recaudos.publicar, _sobre("RecaudoConfirmado", aceptacion["id"])
        )
        await _esperar(
            lambda: _filas(
                limpia,
                "SELECT 1 FROM polizas WHERE documento_version_id IS NOT NULL "
                "AND ultimo_fallo = 'FIRMA_NO_DISPONIBLE'",
            )
        )
        fila = _poliza(limpia, aceptacion["id"])
        respuesta = await api.get(
            f"/v1/cotizaciones/{aceptacion['cotizacionId']}/poliza", headers=ASESOR
        )
        documento = await api.get(f"/v1/polizas/{fila['poliza_id']}/documento", headers=ASESOR)
        verificacion = await api.post(
            VERIFICACIONES,
            json={"numero": fila["numero"], "huella": fila["documento_huella"]},
            headers=ASESOR,
        )

    assert respuesta.status_code == 200
    poliza = respuesta.json()
    assert (poliza["estado"], poliza["emitidaEn"], poliza["firma"]) == ("EMITIENDO", None, None)
    assert poliza["documento"] == {
        "huella": fila["documento_huella"],
        "versionId": fila["documento_version_id"],
    }
    assert (documento.status_code, documento.json()["codigo"]) == (409, "POLIZA_NO_ACTIVA")
    assert verificacion.json() == {
        "valido": False,
        "numero": fila["numero"],
        "firmaId": None,
        "selloTiempo": None,
    }


# --- Sin póliza (AC2) --------------------------------------------------------------------------
async def test_recaudo_pendiente_o_rechazado_sin_poliza(
    abrir_api_emisora: Any,
    cola_recaudos: Any,
    sinteticos: Any,
    limpia: str,
) -> None:
    async with abrir_api_emisora() as api:
        aceptacion = await _aceptar(api, sinteticos)
        ruta = f"/v1/cotizaciones/{aceptacion['cotizacionId']}/poliza"
        pendiente = await api.get(ruta, headers=ASESOR)
        await asyncio.to_thread(
            cola_recaudos.publicar, _sobre("RecaudoRechazado", aceptacion["id"])
        )
        await _esperar(lambda: _filas(limpia, "SELECT 1 FROM recaudos_aceptacion"))
        rechazado = await api.get(ruta, headers=ASESOR)
        sin_aceptacion = await api.get(f"/v1/cotizaciones/{uuid4()}/poliza", headers=ASESOR)

    for respuesta in (pendiente, rechazado):
        assert (respuesta.status_code, respuesta.json()["codigo"]) == (
            404,
            "POLIZA_NO_ENCONTRADA",
        )
    assert (sin_aceptacion.status_code, sin_aceptacion.json()["codigo"]) == (
        404,
        "ACEPTACION_NO_ENCONTRADA",
    )
    assert _filas(limpia, "SELECT 1 FROM polizas") == []
