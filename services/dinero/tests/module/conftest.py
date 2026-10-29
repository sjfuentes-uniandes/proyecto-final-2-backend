"""Arnés de las pruebas de módulo de dinero (HU-W16, 16.9): PostgreSQL efímero (Testcontainers) con
las migraciones del servicio.

`limpia` vacía las tablas antes de cada prueba; `pool` es un `AsyncConnectionPool` sobre esa base y
`recaudos`, el repositorio sobre ese pool.

Desde la 16.10, también LocalStack 4.14.0 (`nueva_cola`: una cola con su DLQ, como
`dinero-aceptaciones`) y un WireMock 3.9.2 en lugar de adaptador-pagos (`adaptador`), con respuestas
fijas según su contrato, elegidas por `pagadorRef`; su journal cuenta los cobros.

psycopg asíncrono no corre sobre el ProactorEventLoop de Windows: las pruebas corren en un
SelectorEventLoop (en Linux es el bucle por defecto), como `polizas`, `cotizacion` y `bff-movil`.
"""

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx
import psycopg
import pytest
from dinero.adapters.outbound.recaudos_pg import RecaudosPg, migrar

docker = pytest.importorskip("docker")


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
    # TRUNCATE no dispara el disparador de protección (es por fila): solo para las pruebas.
    _ejecutar(base, "TRUNCATE recaudos, outbox, inbox")
    return base


@pytest.fixture
async def pool(limpia: str) -> AsyncIterator[Any]:
    from psycopg_pool import AsyncConnectionPool

    async with AsyncConnectionPool(limpia, min_size=1, max_size=25, open=False) as abierto:
        yield abierto


@pytest.fixture
def recaudos(pool: Any) -> RecaudosPg:
    return RecaudosPg(pool)


# --- HU-W16 (16.10): mensajería y adaptador-pagos ----------------------------------------------------
IMAGEN_LOCALSTACK = "localstack/localstack:4.14.0"
IMAGEN_WIREMOCK = "wiremock/wiremock:3.9.2"
REGION = "us-east-1"
ESPERA_LARGA_S = 1
MAX_RECEPCIONES = 5  # maxReceiveCount de dinero-aceptaciones en infra/platform/catalog.tf
# Pagadores sintéticos del simulador de pagos (16.7): el WireMock responde por `pagadorRef`.
C01 = "00000000-0000-4000-8000-000000000001"  # aprobado
C02 = "00000000-0000-4000-8000-000000000002"  # rechazado
C06 = "00000000-0000-4000-8000-000000000006"  # lento (8000 ms): dinero corta a los 6,5 s
C07 = "00000000-0000-4000-8000-000000000007"  # el adaptador responde 503 FUENTE_NO_DISPONIBLE
_CLAVE = "{{jsonPath request.body '$.claveRecaudo'}}"


def _cobro(estado: str, transaccion: str | None, motivo: str | None) -> str:
    # CobroDto de contracts/openapi/adaptador-pagos.yaml, con la claveRecaudo recibida.
    return json.dumps(
        {"claveRecaudo": _CLAVE, "estado": estado, "transaccionId": transaccion, "motivo": motivo}
    )


def _stub(pagador: str, estado: int, cuerpo: str, retardo_ms: int = 0) -> dict[str, Any]:
    respuesta: dict[str, Any] = {
        "status": estado,
        "headers": {"Content-Type": "application/json"},
        "body": cuerpo,
        "transformers": ["response-template"],
    }
    if retardo_ms:
        respuesta["fixedDelayMilliseconds"] = retardo_ms
    return {
        "request": {
            "method": "POST",
            "url": "/v1/cobros",
            "bodyPatterns": [
                {"matchesJsonPath": {"expression": "$.pagadorRef", "equalTo": pagador}}
            ],
        },
        "response": respuesta,
    }


STUBS = [
    _stub(C01, 200, _cobro("APROBADO", "TX-SINT-C01", None)),
    _stub(C02, 200, _cobro("RECHAZADO", None, "FONDOS_INSUFICIENTES")),
    _stub(C06, 200, _cobro("APROBADO", "TX-SINT-C06", None), retardo_ms=8000),
    _stub(
        C07,
        503,
        json.dumps(
            {
                "codigo": "FUENTE_NO_DISPONIBLE",
                "mensaje": "El proveedor no está disponible",
                "correlationId": None,
                "detalles": {"motivo": "ERROR", "circuito": "CERRADO"},
            }
        ),
    ),
]


class WireMock:
    """adaptador-pagos sustituido: la cadena real es la R3 de la 16.12."""

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

    def registrar(self, stubs: list[dict[str, Any]]) -> None:
        for stub in stubs:
            self._admin.post("/mappings", json=stub).raise_for_status()

    def limpiar_journal(self) -> None:
        self._admin.delete("/requests").raise_for_status()

    def cobros(self) -> list[dict[str, Any]]:
        """Las solicitudes a POST /v1/cobros que llegaron, de la más antigua a la más nueva."""
        solicitudes = self._admin.get("/requests").json()["requests"]
        return [
            s["request"]
            for s in reversed(solicitudes)
            if s["request"]["method"] == "POST" and s["request"]["url"] == "/v1/cobros"
        ]


@pytest.fixture(scope="module")
def wiremock() -> Iterator[WireMock]:
    _docker_disponible()
    from testcontainers.core.container import DockerContainer

    contenedor = (
        DockerContainer(IMAGEN_WIREMOCK).with_exposed_ports(8080).with_command("--disable-banner")
    )
    with contenedor:
        servidor = WireMock(
            f"http://{contenedor.get_container_host_ip()}:{contenedor.get_exposed_port(8080)}"
        )
        servidor.esperar_listo()
        servidor.registrar(STUBS)
        yield servidor


@pytest.fixture
def adaptador(wiremock: WireMock) -> WireMock:
    wiremock.limpiar_journal()
    return wiremock


@pytest.fixture(scope="module")
def localstack() -> Iterator[str]:
    _docker_disponible()
    from testcontainers.community.localstack import LocalStackContainer

    with LocalStackContainer(image=IMAGEN_LOCALSTACK, region_name=REGION).with_services(
        "sns", "sqs"
    ) as contenedor:
        yield contenedor.get_url()


@pytest.fixture
def credenciales(monkeypatch: pytest.MonkeyPatch) -> None:
    # LocalStack acepta cualquier credencial; boto3 usa la cadena por defecto.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


@dataclass
class Cola:
    """Una cola nueva con su DLQ (`maxReceiveCount` = 5, como infra)."""

    sqs: Any
    url: str
    dlq: str
    arn: str

    def enviar(self, cuerpo: str, tipo: str = "OfertaAceptada") -> None:
        self.sqs.send_message(
            QueueUrl=self.url,
            MessageBody=cuerpo,
            MessageAttributes={"eventType": {"DataType": "String", "StringValue": tipo}},
        )

    def mensajes(self, url: str) -> int:
        """Visibles más en vuelo: 0 si todo se borró."""
        atributos = self.sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        return sum(int(valor) for valor in atributos.values())

    def receptor(self, max_mensajes: int = 2) -> Any:
        from solventa_common.inbox import ReceptorSqs

        return ReceptorSqs(
            self.sqs, self.url, espera_larga_s=ESPERA_LARGA_S, max_mensajes=max_mensajes
        )


@pytest.fixture
def nueva_cola(localstack: str, credenciales: None) -> Callable[..., Cola]:
    from solventa_common.inbox import cliente_sqs

    sqs = cliente_sqs(REGION, endpoint_url=localstack, espera_larga_s=ESPERA_LARGA_S)

    def crear(visibilidad_s: int = 1, nombre: str = "aceptaciones") -> Cola:
        sufijo = uuid4().hex[:8]
        dlq = sqs.create_queue(QueueName=f"{nombre}-{sufijo}-dlq")["QueueUrl"]
        dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["QueueArn"])["Attributes"][
            "QueueArn"
        ]
        url = sqs.create_queue(
            QueueName=f"{nombre}-{sufijo}",
            Attributes={
                "VisibilityTimeout": str(visibilidad_s),
                "RedrivePolicy": json.dumps(
                    {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": MAX_RECEPCIONES}
                ),
            },
        )["QueueUrl"]
        arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"][
            "QueueArn"
        ]
        return Cola(sqs=sqs, url=url, dlq=dlq, arn=arn)

    return crear
