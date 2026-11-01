"""N3 R3 (HU-W17) contra un ambiente desplegado: de la aceptación a la póliza emitida y su documento.

Recorre el caso nominal de la versión de módulo (`services/polizas/tests/module/
test_r3_emision_w17.py`) con lo que se ve desde fuera: el portal (`/web`, usuario E2E del
back-office en `asesores`) para aceptar y para leer el recaudo, la póliza y la URL de su documento
(17.11); y, solo para lo que el portal no muestra, los logs de CloudWatch
(`/ecs/<prefijo>/<servicio>`) de `polizas` y `adaptador-firma`, por `clave_recaudo` y
`correlation_id`, y el bucket de documentos de póliza (solo lectura). Sin acceso a las bases ni a
las colas: leer `avisos` o `consulta-proyeccion` le quitaría los mensajes a su consumidor.

- C01: por el portal, aceptación de S01 → recaudo `CONFIRMADO` → póliza `ACTIVA`, con su documento
  y su firma → la URL del documento descarga el PDF, cuyo sha256 es la huella de la póliza y cuyo
  `versionId` es el registrado. En CloudWatch, polizas la emitió una sola vez (`emision: póliza
  procesada`, `ok`, con la correlación de la aceptación) y adaptador-firma midió un solo
  `firma.documento` `ok`. En el bucket, una sola versión de `polizas/<polizaId>/<numero>.pdf`: la
  de la póliza, cifrada con el SSE del bucket.

La verificación por la huella (`POST /v1/polizas/verificaciones`) no la expone el portal y la huella
de `PolizaEmitida` no se ve desde fuera: se comprueban solo en la versión de módulo.

C06 (firma lenta) y C07 (firma caída) solo se verifican a nivel de módulo: dejarían pólizas
EMITIENDO en un ambiente compartido y C07 quedaría así para siempre (la tarea emisora la reintenta
y, pasada una hora, la mide como `error`, lo que hace sonar la alarma de `emision`). El aviso
fallido (AC3) también: requiere un consumidor de prueba en `avisos`, que es de HU-M11.

Requiere lo mismo que la R3 de HU-W16 (EXT-01 y EXT-02 en `int`, el usuario E2E en `asesores` y
credenciales que lean los logs) más lectura del bucket de documentos (`s3:ListBucketVersions` y
`s3:GetObjectVersion`, para listar las versiones y leer su cabecera; el PDF se descarga por la URL
que entrega el portal). El bucket y su SSE salen de
`platform.policy_documents_bucket` o de `E2E_POLIZAS_BUCKET` y `E2E_POLIZAS_BUCKET_SSE`. Espera al
recaudo y a la emisión `E2E_EMISION_ESPERA_S` (por defecto, 180). Sin configuración se omite como
las demás N3.

Configuración y precondiciones en infra/DESPLIEGUE.md (sección 7, "Pruebas E2E (N3)").
"""

import hashlib
import json
import os
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

HISTORIA = "HU-W17 (R3)"
TIMEOUT_S = 30
ESPERA_EMISION_S = int(os.getenv("E2E_EMISION_ESPERA_S", "180"))
# Tras la emisión, cuánto se espera a que aparezca una segunda (no debe).
ESPERA_DUPLICADO_S = 60
VERSION_DECLARACIONES = "2026.10.1"
DECLARACIONES = {
    "leidoCondiciones": True,
    "informacionVeraz": True,
    "debitoAutomatico": True,
    "avisosElectronicos": True,
}
# Cliente sintético de S01: pagador y firmante de los simuladores de pagos y de firma.
C01 = "00000000-0000-4000-8000-000000000001"
# El mensaje de polizas (application/poliza/emitir.py).
EMITIDA = "emision: póliza procesada"


class Portal:
    """El portal, por `/web`, con el usuario E2E del back-office (grupo `asesores`)."""

    def __init__(self, base: str, token: str) -> None:
        self._base = f"{base}/web/v1"
        self._cabeceras = {"Authorization": f"Bearer {token}"}

    def post(self, ruta: str, cuerpo: dict[str, Any]) -> httpx.Response:
        respuesta = httpx.post(
            f"{self._base}{ruta}",
            json=cuerpo,
            headers={**self._cabeceras, "Idempotency-Key": str(uuid.uuid4())},
            timeout=TIMEOUT_S,
        )
        assert respuesta.status_code != 403 or "IDENTIDAD" in respuesta.text, (
            "el usuario E2E del back-office debe estar en el grupo asesores (infra/DESPLIEGUE.md)"
        )
        return respuesta

    def get(self, ruta: str) -> httpx.Response:
        respuesta = httpx.get(f"{self._base}{ruta}", headers=self._cabeceras, timeout=TIMEOUT_S)
        assert respuesta.status_code != 403, (
            "el usuario E2E del back-office debe estar en el grupo asesores (infra/DESPLIEGUE.md)"
        )
        return respuesta

    def esperar(
        self, ruta: str, condicion: Callable[[httpx.Response], bool], espera_s: int
    ) -> httpx.Response:
        """La primera respuesta de `ruta` que cumple `condicion` (o la última al vencer)."""
        limite = time.monotonic() + espera_s
        while True:
            respuesta = self.get(ruta)
            if condicion(respuesta) or time.monotonic() > limite:
                return respuesta
            time.sleep(5)

    def aceptar_s01(self) -> httpx.Response:
        """Perfil, cotización, decisión APROBADA y aceptación de S01 (C01, 36 años)."""
        datos = {
            "fechaNacimiento": "1990-04-18",
            "plazoCreditoMeses": 180,
            "sumaAsegurada": 180000000,
        }
        perfil = self.post(
            "/perfiles",
            {"clienteId": C01, "productoId": "vida-hipotecario", "datosDeclarados": datos},
        )
        assert perfil.status_code in (200, 201), perfil.text
        cotizacion = self.post(
            "/cotizaciones",
            {
                "clienteId": C01,
                "productoId": "vida-hipotecario",
                "datosAsegurado": datos,
                "perfilId": perfil.json()["id"],
            },
        )
        assert cotizacion.status_code == 201, cotizacion.text
        decision = self.post("/suscripciones", {"cotizacionId": cotizacion.json()["id"]})
        assert decision.status_code == 201, decision.text
        assert decision.json()["resultado"] == "APROBADA", decision.text
        aceptacion = self.post(
            f"/cotizaciones/{decision.json()['cotizacionId']}/aceptacion",
            {
                "decisionId": decision.json()["id"],
                "versionDeclaraciones": VERSION_DECLARACIONES,
                "declaraciones": DECLARACIONES,
            },
        )
        assert aceptacion.status_code == 201, aceptacion.text
        return aceptacion


class Aws:
    """Los logs de los servicios y el bucket de documentos de póliza (solo lectura)."""

    def __init__(self, region: str, prefijo: str, bucket: str) -> None:
        import boto3

        self._logs = boto3.client("logs", region_name=region)
        self.s3 = boto3.client("s3", region_name=region)
        self._prefijo = prefijo
        self.bucket = bucket

    def registros(
        self, servicio: str, campo: str, valor: str, desde: datetime
    ) -> list[dict[str, Any]]:
        """Las líneas JSON del servicio con `campo` = `valor` desde `desde`."""
        pedido: dict[str, Any] = {
            "logGroupName": f"/ecs/{self._prefijo}/{servicio}",
            "filterPattern": f'{{ $.{campo} = "{valor}" }}',
            "startTime": int(desde.timestamp() * 1000),
        }
        lineas: list[dict[str, Any]] = []
        while True:
            respuesta = self._logs.filter_log_events(**pedido)
            lineas += [json.loads(e["message"]) for e in respuesta["events"]]
            if not respuesta.get("nextToken"):
                return lineas
            pedido["nextToken"] = respuesta["nextToken"]

    def esperar(
        self,
        servicio: str,
        campo: str,
        valor: str,
        desde: datetime,
        condicion: Callable[[dict[str, Any]], bool],
        espera_s: int,
    ) -> list[dict[str, Any]]:
        """Los registros que cumplen `condicion`, en cuanto haya alguno (o vacío al vencer)."""
        limite = time.monotonic() + espera_s
        while True:
            encontrados = [r for r in self.registros(servicio, campo, valor, desde) if condicion(r)]
            if encontrados or time.monotonic() > limite:
                return encontrados
            time.sleep(10)

    def medidos(
        self, servicio: str, operacion: str, correlacion: str, desde: datetime
    ) -> list[str]:
        """Los resultados medidos de `operacion` en el servicio para esa correlación."""
        return [
            r["result"]
            for r in self.registros(servicio, "correlation_id", correlacion, desde)
            if r.get("operation") == operacion and r.get("result")
        ]

    def versiones(self, prefijo: str) -> list[dict[str, Any]]:
        respuesta = self.s3.list_object_versions(Bucket=self.bucket, Prefix=prefijo)
        versiones: list[dict[str, Any]] = respuesta.get("Versions", [])
        return versiones


def es(mensaje: str, **campos: Any) -> Callable[[dict[str, Any]], bool]:
    return lambda r: r.get("message") == mensaje and all(r.get(k) == v for k, v in campos.items())


@pytest.fixture(scope="module")
def portal(ambiente: Any) -> Portal:
    config = ambiente.requerir(
        HISTORIA, {"canales": ambiente.urls["canales"], "admin_token": ambiente.token_admin()}
    )
    return Portal(config["canales"], config["admin_token"])


@pytest.fixture(scope="module")
def documentos(ambiente: Any) -> dict[str, str]:
    """El bucket de documentos de póliza y el SSE que exige su política."""
    bucket = ambiente.salidas.get("platform", {}).get("policy_documents_bucket", {})
    return ambiente.requerir(
        HISTORIA,
        {
            "bucket": os.getenv("E2E_POLIZAS_BUCKET", bucket.get("name", "")),
            "sse": os.getenv("E2E_POLIZAS_BUCKET_SSE", bucket.get("sse", "")),
        },
    )


@pytest.fixture(scope="module")
def aws(ambiente: Any, documentos: dict[str, str]) -> Aws:
    plataforma = ambiente.salidas.get("platform", {})
    config = ambiente.requerir(
        HISTORIA,
        {
            "region": os.getenv("AWS_REGION", plataforma.get("region", "")),
            "prefijo": os.getenv("E2E_PREFIJO", plataforma.get("prefix", "")),
        },
    )
    try:
        return Aws(config["region"], config["prefijo"], documentos["bucket"])
    except ImportError:
        pytest.skip(f"N3 {HISTORIA}: boto3 no está instalado")


def _hace_un_minuto() -> datetime:
    # Margen para el reloj de CloudWatch.
    return datetime.now(UTC) - timedelta(minutes=1)


def test_hu_w17_r3_c01_la_aceptacion_pagada_se_emite_una_vez_con_su_documento(
    portal: Portal, aws: Aws, documentos: dict[str, str]
) -> None:
    desde = _hace_un_minuto()
    aceptacion = portal.aceptar_s01()
    clave = aceptacion.json()["id"]
    cotizacion_id = aceptacion.json()["cotizacionId"]
    correlacion = aceptacion.headers["X-Correlation-Id"]

    # Por el portal: el recaudo confirmado y la póliza ACTIVA, con su documento y su firma.
    recaudo = portal.esperar(
        f"/cotizaciones/{cotizacion_id}/recaudo",
        lambda r: r.status_code == 200 and r.json()["estado"] != "PENDIENTE",
        ESPERA_EMISION_S,
    )
    assert recaudo.status_code == 200, recaudo.text
    assert (recaudo.json()["estado"], recaudo.json()["claveRecaudo"]) == ("CONFIRMADO", clave)
    respuesta = portal.esperar(
        f"/cotizaciones/{cotizacion_id}/poliza",
        lambda r: r.status_code == 200 and r.json()["estado"] == "ACTIVA",
        ESPERA_EMISION_S,
    )
    assert respuesta.status_code == 200, respuesta.text
    poliza = respuesta.json()
    assert poliza["estado"] == "ACTIVA", poliza["estado"]
    assert poliza["documento"] is not None and poliza["firma"] is not None
    assert poliza["emitidaEn"] is not None
    poliza_id = poliza["polizaId"]

    # La URL del documento, por el portal: descarga la versión registrada, cuyo sha256 es la huella.
    enlace = portal.get(f"/polizas/{poliza_id}/documento")
    assert enlace.status_code == 200, enlace.text
    url = enlace.json()["url"]
    assert parse_qs(urlsplit(url).query)["versionId"] == [poliza["documento"]["versionId"]]
    descarga = httpx.get(url, timeout=TIMEOUT_S)
    assert descarga.status_code == 200
    assert descarga.content.startswith(b"%PDF-")
    assert "sha256:" + hashlib.sha256(descarga.content).hexdigest() == poliza["documento"]["huella"]

    # Lo que el portal no muestra. La emisión: una, ok, con la correlación de la aceptación.
    emitidas = aws.esperar(
        "polizas", "clave_recaudo", clave, desde, es(EMITIDA, resultado="ok"), ESPERA_EMISION_S
    )
    assert [(r["correlation_id"], r["poliza_id"]) for r in emitidas] == [(correlacion, poliza_id)]
    time.sleep(ESPERA_DUPLICADO_S)
    procesadas = [
        r
        for r in aws.registros("polizas", "clave_recaudo", clave, desde)
        if r.get("message") == EMITIDA
    ]
    assert [(r["poliza_id"], r["resultado"]) for r in procesadas] == [(poliza_id, "ok")]
    assert aws.medidos("polizas", "emision", correlacion, desde) == ["ok"]
    assert aws.medidos("adaptador-firma", "firma.documento", correlacion, desde) == ["ok"]

    # El documento: una sola versión en el bucket, la de la póliza, cifrada con el SSE del bucket.
    [version] = aws.versiones(f"polizas/{poliza_id}/")
    assert version["Key"] == f"polizas/{poliza_id}/{poliza['numero']}.pdf" and version["IsLatest"]
    assert version["VersionId"] == poliza["documento"]["versionId"]
    assert version["Size"] == len(descarga.content)
    cabecera = aws.s3.head_object(
        Bucket=aws.bucket, Key=version["Key"], VersionId=version["VersionId"]
    )
    assert cabecera["ServerSideEncryption"] == documentos["sse"]
    assert cabecera["ContentType"] == "application/pdf"
    print(
        f"\nHU-W17 R3: póliza {poliza_id} emitida (aceptación {clave}, correlación {correlacion})"
    )
