"""La app completa de siniestros contra PostgreSQL y LocalStack reales (HU-W19): bandeja, tomar el
siguiente, detalle con S3, URL prefirmada y auditoría por Outbox → SNS → SQS."""

import base64
import hashlib
import json
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from siniestros.adapters.outbound.mapeo import copia_a_json, ubicacion_a_json
from siniestros.config import Settings
from siniestros.domain.siniestro.modelos import (
    Evidencia,
    Siniestro,
    TipoEvidencia,
    TipoSoporte,
)
from siniestros.main import crear_app

REGION = "us-east-1"
SSE = "AES256"
ANALISTA = "a0a0a0a0-0000-4000-8000-000000000001"
CABECERAS = {
    "X-Actor-Id": ANALISTA,
    "X-Actor-Rol": "ANALISTA_SINIESTROS",
    "X-Actor-Nombre": "Luis Mora",
}
FOTO = b"\x89PNG-foto-sintetica-del-dano"
FOTO_ALTERADA = b"\x89PNG-otra-foto-distinta"
FACTURA = b"%PDF-factura-sintetica"


def sha(contenido: bytes) -> str:
    return hashlib.sha256(contenido).hexdigest()


def base64_de(contenido: bytes) -> str:
    return base64.b64encode(hashlib.sha256(contenido).digest()).decode()


def insertar(conexion: psycopg.Connection[Any], s: Siniestro, *evidencias: Evidencia) -> None:
    conexion.execute(
        "INSERT INTO siniestros (id, codigo, origen, estado, poliza_id, cliente_id, copia_poliza, "
        "causa, fecha_evento, ubicacion_declarada, reportado_en, analista_id, asignado_en, "
        "version, correlation_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            s.id,
            s.codigo,
            s.origen.value,
            s.estado.value,
            s.poliza_id,
            s.cliente_id,
            Jsonb(copia_a_json(s.copia_poliza)),
            s.causa,
            s.fecha_evento,
            Jsonb(ubicacion_a_json(s.ubicacion_declarada)),
            s.reportado_en,
            s.analista_id,
            s.asignado_en,
            s.version,
            s.correlation_id,
        ),
    )
    for e in evidencias:
        conexion.execute(
            "INSERT INTO evidencias (id, siniestro_id, nombre_archivo, tipo, soporte_tipo, "
            "clave_objeto, sha256, capturada_en, ubicacion, tipo_contenido) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                e.id,
                e.siniestro_id,
                e.nombre_archivo,
                e.tipo.value,
                e.soporte_tipo.value if e.soporte_tipo else None,
                e.clave_objeto,
                e.sha256,
                e.capturada_en,
                Jsonb(ubicacion_a_json(e.ubicacion)) if e.ubicacion else None,
                e.tipo_contenido,
            ),
        )


class Cola:
    """La cola `auditoria` de LocalStack, suscrita sin filtro al tópico de eventos."""

    def __init__(self, sqs: Any, url: str) -> None:
        self.sqs, self.url = sqs, url

    def esperar(self, cantidad: int, segundos: float = 20) -> list[dict[str, Any]]:
        mensajes: list[dict[str, Any]] = []
        limite = time.monotonic() + segundos
        while len(mensajes) < cantidad and time.monotonic() < limite:
            respuesta = self.sqs.receive_message(
                QueueUrl=self.url,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=1,
                MessageAttributeNames=["All"],
            )
            for m in respuesta.get("Messages", []):
                mensajes.append(
                    {
                        "tipo": m["MessageAttributes"]["eventType"]["StringValue"],
                        "cuerpo": m["Body"],
                        "sobre": json.loads(m["Body"]),
                    }
                )
                self.sqs.delete_message(QueueUrl=self.url, ReceiptHandle=m["ReceiptHandle"])
        return mensajes


@pytest.fixture
def mensajeria(localstack: str, credenciales: None) -> tuple[str, Cola]:
    import boto3

    sns = boto3.client("sns", region_name=REGION, endpoint_url=localstack)
    sqs = boto3.client("sqs", region_name=REGION, endpoint_url=localstack)
    topico = sns.create_topic(Name=f"eventos-{uuid4().hex[:8]}")["TopicArn"]
    url = sqs.create_queue(QueueName=f"auditoria-{uuid4().hex[:8]}")["QueueUrl"]
    arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]
    sns.subscribe(
        TopicArn=topico, Protocol="sqs", Endpoint=arn, Attributes={"RawMessageDelivery": "true"}
    )
    return topico, Cola(sqs, url)


@pytest.fixture
def cliente(
    limpia: str,
    localstack: str,
    bucket: str,
    mensajeria: tuple[str, Cola],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    topico, _ = mensajeria
    for nombre, valor in {
        "SOLVENTA_DATABASE_URL": limpia,
        "EVIDENCIAS_BUCKET": bucket,
        "SOLVENTA_AWS_ENDPOINT_URL": localstack,
        "OUTBOX_ENABLED": "true",
        "EVENTS_TOPIC_ARN": topico,
        "SOLVENTA_OUTBOX_INTERVALO_S": "0.2",
        "SOLVENTA_S3_TIMEOUT_MS": "2000",
    }.items():
        monkeypatch.setenv(nombre, valor)
    with TestClient(crear_app(Settings())) as aplicacion:
        yield aplicacion


@pytest.fixture
def caso(
    limpia: str,
    s3: Any,
    bucket: str,
    siniestro: Callable[..., Siniestro],
    evidencia: Callable[..., Evidencia],
) -> dict[str, Any]:
    """EVA-01 con una foto íntegra, una foto alterada, una factura y una foto ausente en S3."""
    ahora = datetime.now(UTC)
    reportado = ahora - timedelta(hours=14)
    libre = siniestro(
        reportado_en=reportado,
        fecha_evento=reportado - timedelta(minutes=41),
        copia_poliza=siniestro().copia_poliza,
    )

    def archivo(nombre: str, contenido: bytes, subido: bytes | None, **extra: Any) -> Evidencia:
        e = evidencia(
            siniestro_id=libre.id,
            nombre_archivo=nombre,
            sha256=sha(contenido),
            capturada_en=reportado - timedelta(minutes=5),
            tipo_contenido="image/png",
            **extra,
        )
        if subido is not None:
            s3.put_object(
                Bucket=bucket,
                Key=e.clave_objeto,
                Body=subido,
                ServerSideEncryption=SSE,
                ChecksumSHA256=base64_de(subido),
            )
        return e

    integra = archivo("foto_1.png", FOTO, FOTO)
    alterada = archivo("foto_2.png", FOTO, FOTO_ALTERADA)
    ausente = archivo("foto_3.png", FOTO, None)
    factura = archivo(
        "factura.pdf",
        FACTURA,
        FACTURA,
        tipo=TipoEvidencia.SOPORTE,
        soporte_tipo=TipoSoporte.FACTURA_COMPRA,
    )
    with psycopg.connect(limpia, autocommit=True) as conexion:
        insertar(conexion, libre, integra, alterada, ausente, factura)
    return {
        "caso": libre,
        "integra": integra,
        "alterada": alterada,
        "ausente": ausente,
        "factura": factura,
    }


def test_recorrido_del_analista(
    cliente: TestClient, caso: dict[str, Any], mensajeria: tuple[str, Cola]
) -> None:
    libre: Siniestro = caso["caso"]
    # 1. La cola general lo muestra; "mi bandeja" aún no.
    cola = cliente.get(
        "/v1/siniestros", params={"bandeja": "SIN_ASIGNAR"}, headers=CABECERAS
    ).json()
    assert [i["codigo"] for i in cola["items"]] == [libre.codigo]
    assert cliente.get("/v1/siniestros", headers=CABECERAS).json()["total"] == 0

    # 2. Abrirlo no lo asigna (AC: abrir un caso no lo asigna).
    antes = cliente.get(f"/v1/siniestros/{libre.id}", headers=CABECERAS)
    assert antes.status_code == 200
    assert antes.json()["analistaId"] is None

    # 3. Tomar el siguiente lo asigna; repetir con la misma clave devuelve el mismo caso.
    clave = {"Idempotency-Key": str(uuid4())}
    tomado = cliente.post("/v1/siniestros/siguiente", headers={**CABECERAS, **clave})
    assert tomado.status_code == 200
    assert (tomado.json()["id"], tomado.json()["estado"]) == (str(libre.id), "EN_EVALUACION")
    assert cliente.post("/v1/siniestros/siguiente", headers={**CABECERAS, **clave}).json()[
        "id"
    ] == str(libre.id)
    assert (
        cliente.post(
            "/v1/siniestros/siguiente", headers={**CABECERAS, "Idempotency-Key": str(uuid4())}
        ).json()["codigo"]
        == "COLA_SIN_CASOS"
    )
    assert cliente.get("/v1/siniestros", headers=CABECERAS).json()["total"] == 1

    # 4. El detalle responde 200 aunque un objeto falte (AC4) y marca la alterada.
    detalle = cliente.get(f"/v1/siniestros/{libre.id}", headers=CABECERAS).json()
    integridad = {e["nombreArchivo"]: e["integridad"] for e in detalle["evidencias"]}
    assert integridad == {
        "foto_1.png": "VERIFICADA",
        "foto_2.png": "ALTERADA",
        "foto_3.png": "NO_DISPONIBLE",
        "factura.pdf": "VERIFICADA",
    }
    assert detalle["coherencia"]["archivosAlterados"] == 1
    assert detalle["coherencia"]["resultado"] == "INCONSISTENTE"
    assert detalle["tramite"][2]["actorNombre"] == "Luis Mora"
    assert {v["codigo"]: v["resultado"] for v in detalle["verificacionCobertura"]}[
        "SOPORTES"
    ] == "PARCIAL"
    assert [p["tipo"] for p in detalle["soportesPendientes"]] == [
        "DOCUMENTO_IDENTIDAD",
        "CERTIFICADO_REPARACION",
    ]
    texto = json.dumps(detalle)
    assert caso["integra"].clave_objeto not in texto
    assert "X-Amz" not in texto

    # 5. La URL prefirmada solo se emite al pedirla, y descarga el objeto.
    pedido = cliente.post(
        f"/v1/siniestros/{libre.id}/evidencias/{caso['integra'].id}/enlace", headers=CABECERAS
    )
    assert pedido.status_code == 200
    enlace = pedido.json()
    assert "X-Amz-Expires=300" in enlace["url"]
    assert httpx.get(enlace["url"]).content == FOTO
    caducidad = datetime.fromisoformat(enlace["expiraEn"])
    assert timedelta(seconds=250) < caducidad - datetime.now(UTC) <= timedelta(seconds=300)
    ausente = cliente.post(
        f"/v1/siniestros/{libre.id}/evidencias/{caso['ausente'].id}/enlace", headers=CABECERAS
    )
    assert (ausente.status_code, ausente.json()["codigo"]) == (404, "EVIDENCIA_NO_DISPONIBLE")

    # 6. Un rol inválido es 403 y se audita.
    denegado = cliente.get(
        f"/v1/siniestros/{libre.id}", headers={"X-Actor-Id": ANALISTA, "X-Actor-Rol": "CLIENTE"}
    )
    assert denegado.status_code == 403

    # 7. Todo quedó en el Outbox y llegó a la cola de auditoría, sin la URL ni la clave del objeto.
    esperados = {
        "SiniestroEnEvaluacion": 1,
        "SiniestroConsultado": 2,
        "EvidenciaConsultada": 1,
        "AccesoSiniestroDenegado": 1,
    }
    recibidos = mensajeria[1].esperar(sum(esperados.values()))
    tipos: dict[str, int] = {}
    for m in recibidos:
        tipos[m["tipo"]] = tipos.get(m["tipo"], 0) + 1
    assert tipos == esperados
    for m in recibidos:
        assert "X-Amz" not in m["cuerpo"]
        assert "http" not in m["cuerpo"]
        assert caso["integra"].clave_objeto not in m["cuerpo"]
        assert "Luis" not in m["cuerpo"]
        assert m["sobre"]["version"] == 1
    denegado_evento = next(m for m in recibidos if m["tipo"] == "AccesoSiniestroDenegado")["sobre"][
        "payload"
    ]
    assert (denegado_evento["motivo"], denegado_evento["origen"], denegado_evento["rol"]) == (
        "ROL_INVALIDO",
        "SINIESTROS",
        "CLIENTE",
    )


def test_la_bandeja_de_un_analista_sin_casos_y_el_resumen(
    cliente: TestClient, caso: dict[str, Any]
) -> None:
    ahora = datetime.now(UTC)
    resumen = cliente.get(
        "/v1/siniestros/resumen",
        params={
            "desde": ahora.replace(hour=0).isoformat(),
            "hasta": (ahora + timedelta(days=1)).isoformat(),
        },
        headers=CABECERAS,
    )
    assert resumen.status_code == 200
    assert (resumen.json()["sinAsignar"], resumen.json()["pendientes"]) == (1, 0)
    assert resumen.json()["compromisoHoras"] == 48


def test_la_aplicacion_arranca_migra_y_atiende_el_health(cliente: TestClient) -> None:
    assert cliente.get("/health").json() == {"status": "ok"}
    assert UUID(cliente.get("/v1/siniestros", headers=CABECERAS).headers["X-Correlation-Id"])
