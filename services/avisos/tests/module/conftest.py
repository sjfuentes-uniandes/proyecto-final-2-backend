"""Arnés de las pruebas de módulo de avisos (HU-M11): PostgreSQL efímero (Testcontainers) con las
migraciones aplicadas.

psycopg asíncrono no corre sobre el ProactorEventLoop de Windows: las pruebas corren en un
SelectorEventLoop (en Linux es el bucle por defecto), como `consulta`.

Desde la N2 (HU-M11, fase 4), también LocalStack (`nueva_cola`: una cola con su DLQ, como `avisos`) y
un WireMock con los perfiles push reales del simulador de aliados (`simulador`), con su journal.
"""

import asyncio
import json
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from avisos.adapters.outbound.entregas_pg import migrar

docker = pytest.importorskip("docker")

IMAGEN_LOCALSTACK = "localstack/localstack:4.14.0"
IMAGEN_WIREMOCK = "wiremock/wiremock:3.9.2"
REGION = "us-east-1"
ESPERA_LARGA_S = 1
MAX_RECEPCIONES = 5  # maxReceiveCount de la cola avisos
PERFILES_PUSH = Path(__file__).parents[4] / "services" / "simulador-aliados" / "mappings" / "push"


@pytest.fixture
def anyio_backend() -> tuple[str, dict[str, Any]]:
    return "asyncio", {"loop_factory": asyncio.SelectorEventLoop}


@pytest.fixture(scope="module")
def base() -> Iterator[str]:
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
    try:
        docker.from_env().ping()
    except docker.errors.DockerException:
        pytest.skip("Docker no está disponible para testcontainers")
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("postgres:16", driver=None) as postgres:
        url: str = postgres.get_connection_url()
        migrar(url)
        yield url


def _docker_disponible() -> None:
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
    try:
        docker.from_env().ping()
    except docker.errors.DockerException:
        pytest.skip("Docker no está disponible para testcontainers")


class Simulador:
    """El simulador push: WireMock con los perfiles de `simulador-aliados/mappings/push`."""

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

    def registrar_perfiles(self) -> None:
        for perfil in sorted(PERFILES_PUSH.glob("*.json")):
            self._admin.post(
                "/mappings", json=json.loads(perfil.read_text("utf-8"))
            ).raise_for_status()

    def limpiar_journal(self) -> None:
        self._admin.delete("/requests").raise_for_status()

    def envios(self, token: str | None = None) -> list[dict[str, Any]]:
        """Los cuerpos de POST /push/v1/envios recibidos (de un token, si se indica)."""
        solicitudes = self._admin.get("/requests").json()["requests"]
        cuerpos = [
            json.loads(s["request"]["body"])
            for s in solicitudes
            if s["request"]["method"] == "POST" and s["request"]["url"] == "/push/v1/envios"
        ]
        return [c for c in cuerpos if token is None or c["tokenDispositivo"] == token]


@pytest.fixture(scope="module")
def _simulador_servidor() -> Iterator[Simulador]:
    _docker_disponible()
    from testcontainers.core.container import DockerContainer

    contenedor = (
        DockerContainer(IMAGEN_WIREMOCK).with_exposed_ports(8080).with_command("--disable-banner")
    )
    with contenedor:
        servidor = Simulador(
            f"http://{contenedor.get_container_host_ip()}:{contenedor.get_exposed_port(8080)}"
        )
        servidor.esperar_listo()
        servidor.registrar_perfiles()
        yield servidor


@pytest.fixture
def simulador(_simulador_servidor: Simulador) -> Simulador:
    _simulador_servidor.limpiar_journal()
    return _simulador_servidor


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

    def enviar(self, cuerpo: str, tipo: str = "PolizaEmitida") -> None:
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

    def crear(visibilidad_s: int = 1) -> Cola:
        sufijo = uuid4().hex[:8]
        dlq = sqs.create_queue(QueueName=f"avisos-{sufijo}-dlq")["QueueUrl"]
        dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["QueueArn"])["Attributes"][
            "QueueArn"
        ]
        url = sqs.create_queue(
            QueueName=f"avisos-{sufijo}",
            Attributes={
                "VisibilityTimeout": str(visibilidad_s),
                "RedrivePolicy": json.dumps(
                    {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": MAX_RECEPCIONES}
                ),
            },
        )["QueueUrl"]
        return Cola(sqs=sqs, url=url, dlq=dlq)

    return crear
