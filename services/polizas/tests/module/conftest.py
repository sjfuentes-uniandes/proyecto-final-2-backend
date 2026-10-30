"""Arnés de las pruebas de módulo de polizas (HU-W13, compartido desde HU-W14): PostgreSQL efímero
(Testcontainers) con las migraciones, y la app con dobles HTTP de cotizacion y catalogo (transporte
simulado de httpx con los contratos publicados).

`api` es la app sobre la base limpia; `abrir_api` abre otra instancia sobre la misma base (con
cachés nuevas), para probar lecturas con los proveedores en otro estado.

HU-W16 (16.6): `mensajeria` crea en LocalStack (la imagen de las pruebas de módulo de
`solventa-common`) un tópico con dos colas en crudo, una con el filtro de `dinero-aceptaciones` y
otra sin filtro, como `auditoria`; `abrir_api_con_relay` abre la app con el relay apuntando ahí.

HU-W16 (16.13): `cola_recaudos` crea otro tópico con `polizas-recaudos` (filtro por el prefijo
`Recaudo`) y su DLQ; `abrir_api_con_consumidor` abre la app con el consumidor sobre esa cola.

HU-W17 (17.7): `localstack_s3` es otro LocalStack, con S3 y KMS y `S3_SKIP_SIGNATURE_VALIDATION=0`.

HU-W17 (17.8): el LocalStack de mensajería también tiene S3 (con la validación de firmas), para que
una app use una sola `AWS_ENDPOINT_URL` con cola, tópico y bucket. `firma` es un WireMock 3.9.2 en
lugar de adaptador-firma, con las respuestas de su contrato (`contracts/openapi/adaptador-firma.yaml`)
y el modo de cada prueba (firmado, lento o caído). `colas_poliza` crea un tópico con las colas de
`avisos` (filtro exacto) y `consulta-proyeccion` (prefijo `Poliza`) de catalog.tf.

psycopg asíncrono no corre sobre el ProactorEventLoop de Windows: las pruebas corren en un
SelectorEventLoop (en Linux es el bucle por defecto), como `cotizacion`, `bff-movil` y `clientes`.
"""

import asyncio
import json
import os
import re
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import boto3
import httpx
import psycopg
import pytest
from polizas.adapters.outbound.aceptaciones_pg import AceptacionesPg
from polizas.adapters.outbound.catalogo_http import CatalogoHttp
from polizas.adapters.outbound.codigos import CodigosPg
from polizas.adapters.outbound.cotizacion_http import CotizacionHttp
from polizas.adapters.outbound.decisiones_pg import DecisionesPg, migrar
from polizas.adapters.outbound.polizas_pg import PolizasLecturaPg
from polizas.adapters.outbound.recaudos_pg import RecaudosPg
from polizas.config import Settings
from polizas.main import Puertos, crear_app

docker = pytest.importorskip("docker")

IMAGEN_LOCALSTACK = "localstack/localstack:4.14.0"
REGION = "us-east-1"
# Filtro de dinero-aceptaciones en infra/platform/catalog.tf.
FILTRO_ACEPTACIONES = {"eventType": ["OfertaAceptada"]}
# HU-W16 (16.13): filtro de polizas-recaudos en catalog.tf.
FILTRO_RECAUDOS = {"eventType": [{"prefix": "Recaudo"}]}
# HU-W17 (17.8): filtros de avisos y consulta-proyeccion en infra/platform/catalog.tf:156 y 162.
FILTRO_AVISOS = {"eventType": ["PolizaEmitida", "RecaudoConfirmado"]}
FILTRO_CONSULTA = {"eventType": [{"prefix": "Poliza"}, {"prefix": "Siniestro"}]}
IMAGEN_WIREMOCK = "wiremock/wiremock:3.9.2"


@pytest.fixture
def anyio_backend() -> tuple[str, dict[str, Any]]:
    # psycopg async no corre sobre el ProactorEventLoop de Windows.
    return "asyncio", {"loop_factory": asyncio.SelectorEventLoop}


def _docker_disponible() -> None:
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
    try:
        docker.from_env().ping()
    except docker.errors.DockerException:
        pytest.skip("Docker no está disponible para testcontainers")


@pytest.fixture(scope="module")
def base() -> Iterator[str]:
    _docker_disponible()
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("postgres:16", driver=None) as postgres:
        url: str = postgres.get_connection_url()
        migrar(url)
        yield url


def _ejecutar(url: str, sql: str) -> None:
    with psycopg.connect(url, autocommit=True) as conexion:
        conexion.execute(sql)


@pytest.fixture
def limpia(base: str) -> str:
    # TRUNCATE no dispara el disparador de inmutabilidad (es por fila): solo para las pruebas.
    _ejecutar(
        base,
        "TRUNCATE polizas, aceptaciones, decisiones_suscripcion, outbox, inbox, "
        "recaudos_aceptacion",
    )
    return base


class Proveedores:
    """Dobles HTTP de cotizacion y catalogo, con su estado (nominal, caído o lento)."""

    def __init__(self, sinteticos: Any) -> None:
        self.insumos = {
            c["insumos"]["cotizacionId"]: c["insumos"] for c in sinteticos.datos["casos"]
        }
        self.reglas = sinteticos.respuesta_reglas()
        self.cotizacion = "nominal"
        self.catalogo = "nominal"

    async def responder(self, pedido: httpx.Request) -> httpx.Response:
        estado = self.cotizacion if pedido.url.host == "cotizacion" else self.catalogo
        if estado == "caido":
            return httpx.Response(503, json={"codigo": "SERVICIO_NO_DISPONIBLE"})
        if estado == "lento":
            await asyncio.sleep(2)
        if pedido.url.host == "catalogo":
            return httpx.Response(200, json=self.reglas)
        cotizacion = re.search(r"/v1/cotizaciones/([^/]+)/", pedido.url.path)
        insumos = self.insumos.get(cotizacion.group(1) if cotizacion else "")
        if insumos is None:
            return httpx.Response(404, json={"codigo": "COTIZACION_NO_ENCONTRADA"})
        return httpx.Response(200, json=insumos)


@asynccontextmanager
async def _api(
    url: str, proveedores: Proveedores, reloj: Any, **configuracion: Any
) -> AsyncIterator[httpx.AsyncClient]:
    from psycopg_pool import AsyncConnectionPool

    settings = Settings(SOLVENTA_DATABASE_URL=url, **configuracion)
    async with (
        AsyncConnectionPool(url, min_size=1, max_size=10, open=False) as pool,
        httpx.AsyncClient(transport=httpx.MockTransport(proveedores.responder)) as salida,
    ):
        puertos = Puertos(
            insumos=CotizacionHttp(salida, "http://cotizacion", settings.cotizacion_timeout_ms),
            reglas=CatalogoHttp(salida, "http://catalogo", settings.catalogo_timeout_ms, reloj),
            decisiones=DecisionesPg(pool),
            codigos=CodigosPg(pool),
            reloj=reloj,
            aceptaciones=AceptacionesPg(pool),
            recaudos=RecaudosPg(pool),
            polizas=PolizasLecturaPg(pool),
        )
        app = crear_app(settings, puertos=puertos, pool=pool)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://polizas"
            ) as cliente,
        ):
            yield cliente


@pytest.fixture
async def api(limpia: str, sinteticos: Any, dobles: Any) -> AsyncIterator[Any]:
    proveedores = Proveedores(sinteticos)
    async with _api(limpia, proveedores, dobles.reloj) as cliente:
        cliente.proveedores = proveedores  # type: ignore[attr-defined]
        yield cliente


@pytest.fixture
def abrir_api(
    limpia: str, dobles: Any
) -> Callable[[Proveedores], AbstractAsyncContextManager[httpx.AsyncClient]]:
    """Otra instancia de la app sobre la misma base, con los proveedores dados."""
    return lambda proveedores: _api(limpia, proveedores, dobles.reloj)


# --- Mensajería (HU-W16, 16.6) ------------------------------------------------------------------
@pytest.fixture(scope="module")
def localstack() -> Iterator[str]:
    _docker_disponible()
    from testcontainers.community.localstack import LocalStackContainer

    # HU-W17 (17.8): con S3 y la validación de firmas, para la emisión de punta a punta.
    contenedor = (
        LocalStackContainer(image=IMAGEN_LOCALSTACK, region_name=REGION)
        .with_services("sns", "sqs", "s3")
        .with_env("S3_SKIP_SIGNATURE_VALIDATION", "0")
    )
    with contenedor:
        yield contenedor.get_url()


@pytest.fixture(scope="module")
def localstack_s3() -> Iterator[str]:
    """HU-W17 (17.7): S3 (y KMS, para `aws:kms`) con la validación de firmas encendida, para que
    una URL prefirmada vencida o alterada responda 403 (exploración §E)."""
    _docker_disponible()
    from testcontainers.community.localstack import LocalStackContainer

    contenedor = (
        LocalStackContainer(image=IMAGEN_LOCALSTACK, region_name=REGION)
        .with_services("s3", "kms")
        .with_env("S3_SKIP_SIGNATURE_VALIDATION", "0")
    )
    with contenedor:
        yield contenedor.get_url()


@pytest.fixture
def credenciales(monkeypatch: pytest.MonkeyPatch) -> None:
    # LocalStack acepta cualquier credencial; el relay usa la cadena por defecto de boto3.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


@dataclass
class Mensajeria:
    """Un tópico nuevo por prueba: `aceptaciones` con el filtro de catalog.tf y `auditoria` sin
    filtro. Las dos con entrega en crudo, como `infra/platform/messaging.tf`."""

    sqs: Any
    topic_arn: str
    aceptaciones: str
    auditoria: str

    def recibir(self, cola: str, cantidad: int, espera_max_s: float = 30) -> list[dict[str, Any]]:
        """Recibe y borra hasta `cantidad` mensajes (bloquea: llamarla en un hilo)."""
        mensajes: list[dict[str, Any]] = []
        limite = time.monotonic() + espera_max_s
        while len(mensajes) < cantidad and time.monotonic() < limite:
            respuesta = self.sqs.receive_message(
                QueueUrl=cola,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=1,
                MessageAttributeNames=["All"],
            )
            for mensaje in respuesta.get("Messages", []):
                mensajes.append(mensaje)
                self.sqs.delete_message(QueueUrl=cola, ReceiptHandle=mensaje["ReceiptHandle"])
        return mensajes

    def vacia(self, cola: str) -> bool:
        respuesta = self.sqs.receive_message(QueueUrl=cola, WaitTimeSeconds=2)
        return not respuesta.get("Messages")


@pytest.fixture
def mensajeria(localstack: str, credenciales: None) -> Mensajeria:
    sns = boto3.client("sns", region_name=REGION, endpoint_url=localstack)
    sqs = boto3.client("sqs", region_name=REGION, endpoint_url=localstack)
    sufijo = uuid4().hex[:8]
    topic_arn = sns.create_topic(Name=f"eventos-{sufijo}")["TopicArn"]

    def cola(nombre: str, filtro: dict[str, Any] | None) -> str:
        url: str = sqs.create_queue(QueueName=f"{nombre}-{sufijo}")["QueueUrl"]
        arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"][
            "QueueArn"
        ]
        atributos = {"RawMessageDelivery": "true"}
        if filtro is not None:
            atributos["FilterPolicy"] = json.dumps(filtro)
        sns.subscribe(TopicArn=topic_arn, Protocol="sqs", Endpoint=arn, Attributes=atributos)
        return url

    return Mensajeria(
        sqs=sqs,
        topic_arn=topic_arn,
        aceptaciones=cola("dinero-aceptaciones", FILTRO_ACEPTACIONES),
        auditoria=cola("auditoria", None),
    )


@dataclass
class ColaRecaudos:
    """HU-W16 (16.13): un tópico nuevo por prueba con `polizas-recaudos` (filtro de catalog.tf,
    entrega en crudo) y su DLQ. Visibilidad de 1 s y 2 recepciones antes de la DLQ (en infra/, 120 s
    y 5), para que un envenenado llegue a la DLQ en segundos."""

    sns: Any
    sqs: Any
    topic_arn: str
    cola: str
    dlq: str

    def publicar(self, sobre: dict[str, Any]) -> None:
        """Publica el sobre como el relay: el cuerpo en crudo y `eventType` = `type`."""
        self.sns.publish(
            TopicArn=self.topic_arn,
            Message=json.dumps(sobre),
            MessageAttributes={"eventType": {"DataType": "String", "StringValue": sobre["type"]}},
        )

    def pendientes(self, cola: str) -> int:
        """Mensajes visibles y en vuelo de la cola (sin recibirlos)."""
        atributos = self.sqs.get_queue_attributes(
            QueueUrl=cola,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        return sum(int(valor) for valor in atributos.values())


@pytest.fixture
def cola_recaudos(localstack: str, credenciales: None) -> ColaRecaudos:
    sns = boto3.client("sns", region_name=REGION, endpoint_url=localstack)
    sqs = boto3.client("sqs", region_name=REGION, endpoint_url=localstack)
    sufijo = uuid4().hex[:8]
    topic_arn = sns.create_topic(Name=f"eventos-{sufijo}")["TopicArn"]
    dlq: str = sqs.create_queue(QueueName=f"polizas-recaudos-dlq-{sufijo}")["QueueUrl"]
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]
    cola: str = sqs.create_queue(
        QueueName=f"polizas-recaudos-{sufijo}",
        Attributes={
            "VisibilityTimeout": "1",
            "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "2"}),
        },
    )["QueueUrl"]
    arn = sqs.get_queue_attributes(QueueUrl=cola, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]
    sns.subscribe(
        TopicArn=topic_arn,
        Protocol="sqs",
        Endpoint=arn,
        Attributes={"RawMessageDelivery": "true", "FilterPolicy": json.dumps(FILTRO_RECAUDOS)},
    )
    return ColaRecaudos(sns=sns, sqs=sqs, topic_arn=topic_arn, cola=cola, dlq=dlq)


@pytest.fixture
def abrir_api_con_consumidor(
    limpia: str, sinteticos: Any, dobles: Any, localstack: str, cola_recaudos: ColaRecaudos
) -> Callable[[], AbstractAsyncContextManager[httpx.AsyncClient]]:
    """La app con el consumidor de `polizas-recaudos` sobre la cola de `cola_recaudos` (espera
    larga de 1 s, para que la parada sea rápida)."""
    return lambda: _api(
        limpia,
        Proveedores(sinteticos),
        dobles.reloj,
        SQS_POLIZAS_RECAUDOS_URL=cola_recaudos.cola,
        AWS_REGION=REGION,
        AWS_ENDPOINT_URL=localstack,
        consumidor_espera_larga_s=1,
    )


@pytest.fixture
def abrir_api_con_relay(
    limpia: str, sinteticos: Any, dobles: Any, localstack: str, mensajeria: Mensajeria
) -> Callable[[bool], AbstractAsyncContextManager[httpx.AsyncClient]]:
    """La app con `OUTBOX_ENABLED` dado y el relay hacia el tópico de `mensajeria`."""
    return lambda habilitado: _api(
        limpia,
        Proveedores(sinteticos),
        dobles.reloj,
        OUTBOX_ENABLED=habilitado,
        EVENTS_TOPIC_ARN=mensajeria.topic_arn,
        AWS_REGION=REGION,
        AWS_ENDPOINT_URL=localstack,
        outbox_intervalo_s=0.1,
    )


# --- HU-W17 (17.8): adaptador-firma en WireMock y las colas de PolizaEmitida -----------------
_DOCUMENTO = "{{jsonPath request.body '$.documentoId'}}"
_HUELLA = "{{jsonPath request.body '$.huella'}}"
# FirmaDto del contrato, con el documento y la huella recibidos (como el simulador).
FIRMADO = json.dumps(
    {
        "documentoId": _DOCUMENTO,
        "estado": "FIRMADO",
        "firmaId": "FIR-SINT-C01",
        "selloTiempo": "2026-01-01T00:00:00Z",
        "huella": _HUELLA,
    }
)
NO_DISPONIBLE = json.dumps(
    {
        "codigo": "FUENTE_NO_DISPONIBLE",
        "mensaje": "El proveedor no está disponible",
        "correlationId": None,
        "detalles": {"motivo": "ERROR", "circuito": "CERRADO"},
    }
)


class FirmaWireMock:
    """adaptador-firma sustituido; `modo` cambia la respuesta de `POST /v1/firmas`."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._admin = httpx.Client(base_url=f"{url}/__admin", timeout=5)

    def esperar_listo(self, segundos: float = 60) -> None:
        limite = time.monotonic() + segundos
        while time.monotonic() < limite:
            try:
                if self._admin.get("/health").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        raise RuntimeError("WireMock no arrancó")

    def modo(self, modo: str, retardo_ms: int = 0) -> None:
        """`firmado` (200, con `retardo_ms`), `lento` (el 200 en 8000 ms: polizas corta a los
        6,5 s) o `caido` (503 FUENTE_NO_DISPONIBLE)."""
        # Solo los stubs: /mappings/reset vaciaría también el diario de solicitudes.
        self._admin.delete("/mappings").raise_for_status()
        estado, cuerpo = (503, NO_DISPONIBLE) if modo == "caido" else (200, FIRMADO)
        respuesta: dict[str, Any] = {
            "status": estado,
            "headers": {"Content-Type": "application/json"},
            "body": cuerpo,
            "transformers": ["response-template"],
        }
        retardo = 8000 if modo == "lento" else retardo_ms
        if retardo:
            respuesta["fixedDelayMilliseconds"] = retardo
        stub = {"request": {"method": "POST", "url": "/v1/firmas"}, "response": respuesta}
        self._admin.post("/mappings", json=stub).raise_for_status()

    def limpiar_journal(self) -> None:
        self._admin.delete("/requests").raise_for_status()

    def firmas(self) -> list[dict[str, Any]]:
        """Las solicitudes a POST /v1/firmas, de la más antigua a la más nueva."""
        solicitudes = self._admin.get("/requests").json()["requests"]
        return [
            s["request"]
            for s in reversed(solicitudes)
            if s["request"]["method"] == "POST" and s["request"]["url"] == "/v1/firmas"
        ]


@pytest.fixture(scope="module")
def wiremock_firma() -> Iterator[FirmaWireMock]:
    _docker_disponible()
    from testcontainers.core.container import DockerContainer

    contenedor = (
        DockerContainer(IMAGEN_WIREMOCK).with_exposed_ports(8080).with_command("--disable-banner")
    )
    with contenedor:
        servidor = FirmaWireMock(
            f"http://{contenedor.get_container_host_ip()}:{contenedor.get_exposed_port(8080)}"
        )
        servidor.esperar_listo()
        yield servidor


@pytest.fixture
def firma(wiremock_firma: FirmaWireMock) -> FirmaWireMock:
    wiremock_firma.modo("firmado")
    wiremock_firma.limpiar_journal()
    return wiremock_firma


@dataclass
class ColasPoliza:
    """Un tópico nuevo por prueba con `avisos` y `consulta-proyeccion` (filtros de catalog.tf,
    entrega en crudo, como messaging.tf)."""

    mensajeria: Mensajeria
    avisos: str
    consulta: str


@pytest.fixture
def colas_poliza(localstack: str, credenciales: None) -> ColasPoliza:
    sns = boto3.client("sns", region_name=REGION, endpoint_url=localstack)
    sqs = boto3.client("sqs", region_name=REGION, endpoint_url=localstack)
    sufijo = uuid4().hex[:8]
    topic_arn = sns.create_topic(Name=f"eventos-poliza-{sufijo}")["TopicArn"]

    def cola(nombre: str, filtro: dict[str, Any]) -> str:
        url: str = sqs.create_queue(QueueName=f"{nombre}-{sufijo}")["QueueUrl"]
        arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"][
            "QueueArn"
        ]
        atributos = {"RawMessageDelivery": "true", "FilterPolicy": json.dumps(filtro)}
        sns.subscribe(TopicArn=topic_arn, Protocol="sqs", Endpoint=arn, Attributes=atributos)
        return url

    avisos, consulta = cola("avisos", FILTRO_AVISOS), cola("consulta-proyeccion", FILTRO_CONSULTA)
    mensajeria = Mensajeria(sqs=sqs, topic_arn=topic_arn, aceptaciones=avisos, auditoria=consulta)
    return ColasPoliza(mensajeria=mensajeria, avisos=avisos, consulta=consulta)


@pytest.fixture
def bucket(localstack: str, credenciales: None) -> str:
    """Un bucket versionado nuevo por prueba en el LocalStack de mensajería."""
    s3 = boto3.client("s3", region_name=REGION, endpoint_url=localstack)
    nombre = f"polizas-{uuid4().hex[:16]}"
    s3.create_bucket(Bucket=nombre)
    s3.put_bucket_versioning(Bucket=nombre, VersioningConfiguration={"Status": "Enabled"})
    return nombre


@pytest.fixture
def abrir_api_emisora(
    limpia: str,
    sinteticos: Any,
    dobles: Any,
    localstack: str,
    cola_recaudos: ColaRecaudos,
    colas_poliza: ColasPoliza,
    firma: FirmaWireMock,
    bucket: str,
) -> Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]]:
    """La app con el consumidor de `polizas-recaudos`, la tarea emisora (bucket del LocalStack y
    adaptador-firma en WireMock) y, si se pide, el relay hacia el tópico de `colas_poliza`. Ciclos
    cortos para que la prueba no espere; el reloj es el de los dobles."""

    def abrir(
        relay: bool = False, **cambios: Any
    ) -> AbstractAsyncContextManager[httpx.AsyncClient]:
        configuracion: dict[str, Any] = {
            "SQS_POLIZAS_RECAUDOS_URL": cola_recaudos.cola,
            "AWS_REGION": REGION,
            "AWS_ENDPOINT_URL": localstack,
            "POLIZAS_BUCKET": bucket,
            "ADAPTADOR_FIRMA_URL": firma.url,
            "OUTBOX_ENABLED": relay,
            "EVENTS_TOPIC_ARN": colas_poliza.mensajeria.topic_arn,
            "consumidor_espera_larga_s": 1,
            "outbox_intervalo_s": 0.1,
            "emision_intervalo_s": 0.1,
        }
        return _api(limpia, Proveedores(sinteticos), dobles.reloj, **(configuracion | cambios))

    return abrir
