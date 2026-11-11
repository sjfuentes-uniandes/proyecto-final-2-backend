"""N3 R5 (HU-W21) contra un ambiente desplegado: del evento firmado a su evaluación.

Hace lo que la versión de módulo y el recorrido local de B4 ya verificaron con las piezas reales,
pero por la entrada pública: firma cada caso de `contracts/datos-sinteticos/eventos-parametricos.json`
con `scripts/dev/firmar_evento.py`, lo envía a `POST /telemetria/v1/eventos` (API Gateway, sin JWT) y
comprueba cada decisión.

- C01: los envíos rechazados (firma alterada o ausente, fuente desconocida, marca de tiempo de hace
  5 min + 1 s o 5 min + 1 s en el futuro, cuerpo malformado, versión no soportada, unidad incoherente
  y campo de más) responden 401 o 422 con su código estable (AC3, SEC-W21-01).
- C02: los ocho eventos válidos responden 202 y el reenvío del primero, 200 con el mismo `eventoId`
  (AC4).
- C03: cada decisión (`ACTIVA`, `NO_ACTIVA` o `SIN_POLIZAS`, con su razón y cuántas pólizas evaluó y
  activó) llega a `auditoria` como `EventoParametricoEvaluado`, **una sola vez** por evento (AC1, AC2
  y AC4). Lee los lotes `cadena/*.json` del bucket de auditoría, el único rastro que un ambiente
  compartido deja ver desde fuera: las tablas de `pagos-parametricos` no son accesibles.

Requiere (sin ellos se omite, como las demás N3):

- `E2E_TELEMETRIA_SECRETO`: el `hmac_secret` de prueba del aliado `telemetria` (Secrets Manager). Lo
  entrega la persona; esta prueba no lo lee de AWS. `E2E_TELEMETRIA_URL` reemplaza la URL del borde
  (por omisión, `<canales>/telemetria/v1/eventos`).
- C03 además: `E2E_PUBLICAR_COBERTURAS=1` (**escribe en el tópico de eventos de INT**: publica las diez
  coberturas sintéticas como las emitiría `polizas`, que todavía no emite `coberturaParametrica`;
  quedan en la copia local de `pagos-parametricos`), credenciales de AWS con `sns:Publish` y lectura
  del bucket de auditoría, y las salidas `events_topic_arn` y `audit_bucket` de `infra/platform`.

Cada ejecución usa un sufijo propio en el `idExterno` (`E2E_TELEMETRIA_SUFIJO` lo fija): sin él, repetir
la prueba sería un reenvío y no un evento nuevo. Espera a la auditoría `E2E_AUDITORIA_ESPERA_S` (por
omisión 180 s: relay del Outbox, dos colas y el lote de auditoría) y deja `E2E_COBERTURAS_ESPERA_S` (20 s)
para que `pagos-parametricos` aplique las coberturas antes de enviar los eventos.
"""

import importlib.util
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

HISTORIA = "HU-W21 (R5)"
RAIZ = Path(__file__).parents[2]
DATOS = RAIZ / "contracts/datos-sinteticos/eventos-parametricos.json"
SCRIPT = RAIZ / "scripts/dev/firmar_evento.py"
ESPERA_AUDITORIA_S = int(os.getenv("E2E_AUDITORIA_ESPERA_S", "180"))
ESPERA_COBERTURAS_S = int(os.getenv("E2E_COBERTURAS_ESPERA_S", "20"))
TIMEOUT_S = 30
# Los eventoId de C02, para que C03 busque sus decisiones.
EVENTOS: dict[str, dict[str, Any]] = {}


@pytest.fixture(scope="module")
def firmador() -> ModuleType:
    """`scripts/dev/firmar_evento.py`: la fuente de telemetría sintética."""
    especificacion = importlib.util.spec_from_file_location("firmar_evento_e2e", SCRIPT)
    assert especificacion and especificacion.loader
    modulo = importlib.util.module_from_spec(especificacion)
    sys.modules["firmar_evento_e2e"] = modulo
    especificacion.loader.exec_module(modulo)
    return modulo


@pytest.fixture(scope="module")
def datos() -> dict[str, Any]:
    cargados: dict[str, Any] = json.loads(DATOS.read_text(encoding="utf-8"))
    return cargados


@pytest.fixture(scope="module")
def sufijo() -> str:
    return os.getenv("E2E_TELEMETRIA_SUFIJO") or uuid.uuid4().hex[:10]


@pytest.fixture(scope="module")
def config(ambiente: Any) -> dict[str, str]:
    base = os.getenv("E2E_TELEMETRIA_URL") or (
        f"{ambiente.urls['canales'].rstrip('/')}/telemetria/v1/eventos"
        if ambiente.urls["canales"]
        else ""
    )
    return ambiente.requerir(
        HISTORIA,
        {"url": base, "secreto": os.getenv("E2E_TELEMETRIA_SECRETO", "")},
    )


class Auditoria:
    """Los lotes `cadena/*.json` del bucket de auditoría y el tópico de eventos de negocio."""

    def __init__(self, salidas: dict[str, Any]) -> None:
        import boto3

        self.bucket: str = salidas["audit_bucket"]["name"]
        self.topico: str = salidas["events_topic_arn"]
        self._s3 = boto3.client("s3")
        self._sns = boto3.client("sns")
        self.desde = datetime.now(UTC)

    def publicar(self, sobre: dict[str, Any]) -> None:
        self._sns.publish(
            TopicArn=self.topico,
            Message=json.dumps(sobre),
            MessageAttributes={"eventType": {"DataType": "String", "StringValue": sobre["type"]}},
        )

    def registros(self) -> list[dict[str, Any]]:
        """Los registros de los lotes escritos desde que empezó la prueba."""
        encontrados: list[dict[str, Any]] = []
        paginas = self._s3.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix="cadena/"
        )
        for pagina in paginas:
            for objeto in pagina.get("Contents", []):
                if objeto["LastModified"] < self.desde:
                    continue
                lote = json.loads(
                    self._s3.get_object(Bucket=self.bucket, Key=objeto["Key"])["Body"].read()
                )
                encontrados.extend(lote["registros"])
        return encontrados

    def esperar(self, condicion: Any, espera_s: int = ESPERA_AUDITORIA_S) -> list[dict[str, Any]]:
        limite = time.monotonic() + espera_s
        while True:
            registros = self.registros()
            if condicion(registros) or time.monotonic() > limite:
                return registros
            time.sleep(5)


@pytest.fixture(scope="module")
def auditoria(ambiente: Any) -> Auditoria:
    if os.getenv("E2E_PUBLICAR_COBERTURAS") != "1":
        pytest.skip(
            "N3 HU-W21 C03: falta E2E_PUBLICAR_COBERTURAS=1 (publica las coberturas sintéticas en "
            "el tópico de eventos de INT)"
        )
    salidas = ambiente.salidas
    if not (salidas.get("audit_bucket", {}).get("name") and salidas.get("events_topic_arn")):
        pytest.skip(
            "N3 HU-W21 C03: faltan las salidas audit_bucket y events_topic_arn de infra/platform"
        )
    try:
        return Auditoria(salidas)
    except Exception as error:  # noqa: BLE001 - sin credenciales de AWS no hay auditoría que leer
        pytest.skip(f"N3 HU-W21 C03: sin acceso a AWS ({type(error).__name__})")


@pytest.fixture(scope="module")
def coberturas(auditoria: Auditoria, datos: dict[str, Any]) -> None:
    """Publica las coberturas sintéticas (idempotente: mismo `eventId`) y espera a que lleguen."""
    ids = {c["sobre"]["eventId"] for c in datos["coberturas"]}
    for entrada in datos["coberturas"]:
        auditoria.publicar(entrada["sobre"])
    registros = auditoria.esperar(
        lambda r: ids <= {x["eventId"] for x in r if x["accion"] == "PolizaEmitida"}
    )
    llegaron = {x["eventId"] for x in registros if x["accion"] == "PolizaEmitida"}
    assert ids <= llegaron, "las coberturas sintéticas no llegaron a auditoría"
    time.sleep(ESPERA_COBERTURAS_S)  # que pagos-parametricos las aplique antes de los eventos


def _enviar(
    firmador: ModuleType, config: dict[str, str], caso: dict[str, Any], sufijo: str
) -> httpx.Response:
    envio = firmador.con_sufijo(caso, sufijo)
    encabezados, cuerpo = firmador.armar_envio(envio, datetime.now(UTC), config["secreto"])
    return httpx.post(
        config["url"],
        content=cuerpo,
        headers={**encabezados, "Content-Type": "application/json"},
        timeout=TIMEOUT_S,
    )


def test_hu_w21_r5_c01_los_envios_rechazados_responden_su_codigo_estable(
    config: dict[str, str], firmador: ModuleType, datos: dict[str, Any], sufijo: str
) -> None:
    rechazados = [c for c in datos["eventos"] if c["esperado"]["http"]["codigo"]]
    assert len(rechazados) == 10
    for caso in rechazados:
        esperado = caso["esperado"]["http"]
        respuesta = _enviar(firmador, config, caso, sufijo)
        assert respuesta.status_code == esperado["estado"], caso["clave"]
        cuerpo = respuesta.json()
        assert cuerpo["codigo"] == esperado["codigo"], caso["clave"]
        assert set(cuerpo) == {"codigo", "mensaje", "correlationId", "detalles"}
        # Ni el secreto, ni la firma, ni el cuerpo vuelven en el error.
        assert config["secreto"] not in respuesta.text and "sha256=" not in respuesta.text


def test_hu_w21_r5_c02_los_eventos_validos_responden_202_y_el_reenvio_200(
    config: dict[str, str],
    firmador: ModuleType,
    datos: dict[str, Any],
    sufijo: str,
    request: pytest.FixtureRequest,
) -> None:
    # Con las coberturas ya publicadas (si C03 está habilitado), los eventos se evalúan contra ellas.
    if os.getenv("E2E_PUBLICAR_COBERTURAS") == "1":
        request.getfixturevalue("coberturas")
    for caso in datos["eventos"]:
        esperado = caso["esperado"]["http"]
        if esperado["codigo"]:
            continue
        respuesta = _enviar(firmador, config, caso, sufijo)
        assert respuesta.status_code == esperado["estado"], caso["clave"]
        cuerpo = respuesta.json()
        assert uuid.UUID(cuerpo["eventoId"]) and cuerpo["recibidoEn"].endswith("Z")
        if esperado["estado"] == 202:
            EVENTOS[caso["clave"]] = cuerpo
        else:
            # AC4: el reenvío conserva el eventoId y el instante del primero.
            assert cuerpo == EVENTOS[caso["reenvioDe"]], caso["clave"]
    assert len(EVENTOS) == 8


def test_hu_w21_r5_c03_cada_decision_llega_a_auditoria_una_sola_vez(
    auditoria: Auditoria, coberturas: None, datos: dict[str, Any]
) -> None:
    assert len(EVENTOS) == 8, "C02 debe correr antes que C03"
    por_evento = {cuerpo["eventoId"]: clave for clave, cuerpo in EVENTOS.items()}
    registros = auditoria.esperar(
        lambda r: (
            {x["resourceId"] for x in r if x["accion"] == "EventoParametricoEvaluado"}
            >= set(por_evento)
        )
    )
    evaluados = [x for x in registros if x["accion"] == "EventoParametricoEvaluado"]
    casos = {c["clave"]: c for c in datos["eventos"]}
    for evento_id, clave in por_evento.items():
        propios = [x for x in evaluados if x["resourceId"] == evento_id]
        # AC4: una sola evaluación, aunque el evento se haya reenviado.
        assert len(propios) == 1, f"{clave}: {len(propios)} evaluaciones en auditoría"
        sobre = propios[0]["evento"]
        sobre = json.loads(sobre) if isinstance(sobre, str) else sobre
        payload = sobre["payload"]
        esperado = casos[clave]["esperado"]["evaluacion"]
        assert (payload["decision"], payload["razon"]) == (
            esperado["decision"],
            esperado["razonEvento"],
        ), clave
        assert (payload["evaluadas"], payload["activaciones"]) == (
            esperado["evaluadas"],
            esperado["activaciones"],
        ), clave
        # SEC-W21-01: la fuente y el idExterno de la evaluación; sin valores ni pólizas.
        assert payload["fuente"] == casos[clave]["envio"]["fuente"]
        assert payload["idExterno"].startswith(casos[clave]["envio"]["cuerpo"]["idExterno"])
        assert "valorObservado" not in payload and "polizaId" not in json.dumps(payload)
