"""N2 (HU-W18): del evento `PolizaEmitida` al estado de la póliza en el portal.

Cadena real, sin simulaciones propias: SQS en LocalStack -> consumidor de `consulta` (inbox y
proyección sobre PostgreSQL real) -> API de `consulta` servida con uvicorn -> `bff-web` (cliente
HTTP real, corte de 700 ms). Solo el catálogo es un transporte simulado. Los eventos son sintéticos
y conformes a `contracts/events/PolizaEmitida.v1.schema.json`.

Determinismo: antes de afirmar cualquier cosa, las pruebas precalientan la conexión de `bff-web` a
`consulta` (importaciones diferidas, pool y primera consulta), de modo que el corte de 700 ms no
produzca fallos por arranque en frío. La reproducción del `503` detiene el servidor ya caliente.
"""

import asyncio
import json
import os
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import psycopg
import pytest
import uvicorn
from bff_web.adapters.inbound import estado_polizas as ruta
from bff_web.adapters.outbound.consulta import ConsultaCliente
from bff_web.application.estado_poliza import EstadoPolizas
from bff_web.config import settings
from bff_web.main import app as portal_app
from consulta.adapters.outbound.catalogo_http import CatalogoHttp
from consulta.config import Settings as SettingsConsulta
from consulta.main import crear_app
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

docker = pytest.importorskip("docker")

EVENTOS = Path(__file__).parents[4] / "contracts/events"
BACKOFFICE = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_back"
ASESOR = {
    "X-Authenticated-Groups": "[asesores]",
    "X-Authenticated-Issuer": BACKOFFICE,
    "X-Authenticated-Sub": "sub-asesora",
}
SUSCRIPTOR = {**ASESOR, "X-Authenticated-Groups": "[suscriptores]"}
REGION = "us-east-1"
IMAGEN_LOCALSTACK = "localstack/localstack:4.14.0"
ESPERA_S = 30
HOY = datetime.now(ZoneInfo("America/Bogota")).date()
_numeros = iter(range(1, 10**6))


def _producto() -> dict[str, Any]:
    return {
        "id": "vida-hipotecario",
        "version": "1.0.0",
        "nombre": "Vida hipotecario",
        "ramo": "VIDA",
        "pais": "CO",
        "moneda": "COP",
        "estado": "ACTIVO",
        "vigenteDesde": "2026-01-01T00:00:00Z",
        "vigenteHasta": None,
        "coberturas": [{"codigo": "MUERTE", "nombre": "Muerte", "obligatoria": True}],
    }


def _evento(desde: date, hasta: date) -> dict[str, Any]:
    """Un `PolizaEmitida` conforme al esquema, emitido ahora, con la vigencia indicada."""
    ahora = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    poliza_id = str(uuid4())
    sobre = {
        "eventId": str(uuid4()),
        "aggregateId": poliza_id,
        "type": "PolizaEmitida",
        "version": 1,
        "occurredAt": ahora,
        "correlationId": "corr-n2-w18",
        "payload": {
            "polizaId": poliza_id,
            "numero": f"100-2026-{next(_numeros):06d}",
            "cotizacionId": str(uuid4()),
            "claveRecaudo": str(uuid4()),
            "clienteId": str(uuid4()),
            "producto": {"id": "vida-hipotecario", "version": "1.0.0"},
            "ramo": {"codigo": "100", "nombre": "Vida hipotecario"},
            "coberturas": [{"codigo": "MUERTE", "nombre": "Muerte", "obligatoria": True}],
            "vigencia": {"desde": desde.isoformat(), "hasta": hasta.isoformat()},
            "prima": "64638.10",
            "sumaAsegurada": "50000000",
            "moneda": "COP",
            "periodicidad": "MENSUAL",
            "documento": {
                "clave": f"polizas/{poliza_id}.pdf",
                "versionId": "v1",
                "huella": "sha256:" + "a" * 64,
                "tamanoBytes": 2048,
            },
            "firma": {"firmaId": "FIRMA-0001", "selloTiempo": ahora},
            "emitidaEn": ahora,
        },
    }
    esquema = json.loads((EVENTOS / "PolizaEmitida.v1.schema.json").read_text(encoding="utf-8"))
    assert list(Draft202012Validator(esquema).iter_errors(sobre)) == []
    return sobre


@contextmanager
def _servir(app: Any) -> Iterator[str]:
    zocalo = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    zocalo.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{zocalo.getsockname()[1]}"
    servidor = uvicorn.Server(uvicorn.Config(app, log_level="warning"))

    def correr() -> None:
        # psycopg asíncrono no corre sobre el ProactorEventLoop de Windows.
        asyncio.run(servidor.serve(sockets=[zocalo]), loop_factory=asyncio.SelectorEventLoop)

    hilo = threading.Thread(target=correr, daemon=True)
    hilo.start()
    limite = time.monotonic() + 60
    while not servidor.started:
        assert hilo.is_alive(), "el servidor no arrancó"
        assert time.monotonic() < limite, "el servidor no arrancó a tiempo"
        time.sleep(0.05)
    try:
        yield url
    finally:
        servidor.should_exit = True
        hilo.join(timeout=20)


class Entorno:
    def __init__(self, base: str, sqs: Any, cola: str, url_consulta: str) -> None:
        self.base = base
        self.sqs = sqs
        self.cola = cola
        self.url_consulta = url_consulta

    def publicar(self, sobre: dict[str, Any]) -> None:
        self.sqs.send_message(
            QueueUrl=self.cola,
            MessageBody=json.dumps(sobre),
            MessageAttributes={"eventType": {"DataType": "String", "StringValue": sobre["type"]}},
        )

    def filas(self, sql: str, parametros: Any = None) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.base, autocommit=True) as conexion:
            return conexion.execute(sql, parametros).fetchall()

    def mensajes_en_cola(self) -> int:
        atributos = self.sqs.get_queue_attributes(
            QueueUrl=self.cola,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        return sum(int(valor) for valor in atributos.values())


@pytest.fixture(scope="module")
def entorno() -> Iterator[Entorno]:
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
    try:
        docker.from_env().ping()
    except docker.errors.DockerException:
        pytest.skip("Docker no está disponible para testcontainers")
    from solventa_common.inbox import cliente_sqs
    from testcontainers.community.localstack import LocalStackContainer
    from testcontainers.community.postgres import PostgresContainer

    anteriores = dict(os.environ)
    os.environ.update({"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test"})
    try:
        with (
            PostgresContainer("postgres:16", driver=None) as postgres,
            LocalStackContainer(image=IMAGEN_LOCALSTACK, region_name=REGION).with_services(
                "sqs"
            ) as localstack,
        ):
            base: str = postgres.get_connection_url()
            sqs = cliente_sqs(REGION, endpoint_url=localstack.get_url(), espera_larga_s=1)
            cola = sqs.create_queue(QueueName=f"consulta-proyeccion-{uuid4().hex[:8]}")["QueueUrl"]
            os.environ.update(
                {
                    "SOLVENTA_DATABASE_URL": base,
                    "SOLVENTA_SQS_CONSULTA_PROYECCION_URL": cola,
                    "SOLVENTA_AWS_ENDPOINT_URL": localstack.get_url(),
                    "SOLVENTA_AWS_REGION": REGION,
                }
            )
            transporte = httpx.MockTransport(
                lambda _: httpx.Response(200, content=json.dumps(_producto()))
            )
            catalogo = CatalogoHttp(httpx.AsyncClient(transport=transporte), "http://catalogo", 300)
            config = SettingsConsulta()
            config.consumidor_espera_larga_s = 1
            with _servir(crear_app(config, catalogo=catalogo)) as url:
                yield Entorno(base, sqs, cola, url)
    finally:
        os.environ.clear()
        os.environ.update(anteriores)


@contextmanager
def _portal(url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(settings, "jwt_issuers", {"backoffice": BACKOFFICE})
    servicio = EstadoPolizas(ConsultaCliente(url, settings.consulta_timeout_ms))
    portal_app.dependency_overrides[ruta.estados] = lambda: servicio
    try:
        cliente = TestClient(portal_app)
        # Precalentamiento: importaciones diferidas, pool y primera consulta, fuera del corte.
        for destino in ("/v1/polizas", f"/v1/polizas/{uuid4()}/estado"):
            for _ in range(3):
                cliente.get(destino, headers=ASESOR)
        yield cliente
    finally:
        portal_app.dependency_overrides.clear()


@pytest.fixture
def portal(entorno: Entorno, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with _portal(entorno.url_consulta, monkeypatch) as cliente:
        yield cliente


def _esperar(condicion: Callable[[], Any], que: str) -> Any:
    limite = time.monotonic() + ESPERA_S
    while True:
        resultado = condicion()
        if resultado:
            return resultado
        assert time.monotonic() < limite, f"no ocurrió a tiempo: {que}"
        time.sleep(0.2)


def _proyectar(entorno: Entorno, portal: TestClient, sobre: dict[str, Any]) -> httpx.Response:
    """Publica el evento y espera a que el portal lo vea; devuelve esa respuesta 200."""
    poliza_id = sobre["payload"]["polizaId"]
    entorno.publicar(sobre)
    return _esperar(
        lambda: (
            (r := portal.get(f"/v1/polizas/{poliza_id}/estado", headers=ASESOR)).status_code == 200
            and r
        ),
        f"proyección de {poliza_id}",
    )


def _comparable(respuesta: httpx.Response) -> dict[str, Any]:
    cuerpo: dict[str, Any] = respuesta.json()
    return {k: v for k, v in cuerpo.items() if k != "correlationId"}


def test_n2_1_no_proyectada_es_404_y_tras_proyectarse_es_200_no_iniciada(
    entorno: Entorno, portal: TestClient
) -> None:
    sobre = _evento(HOY + timedelta(days=1), HOY + timedelta(days=365))
    poliza_id = sobre["payload"]["polizaId"]

    antes = portal.get(f"/v1/polizas/{poliza_id}/estado", headers=ASESOR)
    inexistente = portal.get(f"/v1/polizas/{uuid4()}/estado", headers=ASESOR)
    assert antes.status_code == 404
    assert _comparable(antes) == _comparable(inexistente)
    assert antes.json()["codigo"] == "POLIZA_NO_ENCONTRADA"

    despues = _proyectar(entorno, portal, sobre)
    cuerpo = despues.json()
    # Una póliza recién emitida empieza mañana: todavía no está en vigor.
    assert cuerpo["estado"] == "NO_INICIADA"
    assert cuerpo["numero"] == sobre["payload"]["numero"]
    assert "clienteId" not in cuerpo

    (fila,) = entorno.filas(
        "SELECT emitida_en, proyectada_en FROM poliza_cliente WHERE poliza_id = %s", (poliza_id,)
    )
    emitida, proyectada = fila
    assert datetime.fromisoformat(cuerpo["fechaActualizacion"]) == proyectada
    desfase = (proyectada - emitida).total_seconds()
    assert 0 <= desfase < ESPERA_S
    print(f"N2 desfase emitida_en -> proyectada_en: {desfase:.3f} s")


def test_n2_2_una_poliza_ya_vigente_responde_vigente(entorno: Entorno, portal: TestClient) -> None:
    sobre = _evento(HOY - timedelta(days=30), HOY + timedelta(days=335))
    respuesta = _proyectar(entorno, portal, sobre)
    assert respuesta.json()["estado"] == "VIGENTE"


def test_n2_3_una_poliza_proxima_a_terminar_responde_por_vencer(
    entorno: Entorno, portal: TestClient
) -> None:
    sobre = _evento(HOY - timedelta(days=355), HOY + timedelta(days=10))
    respuesta = _proyectar(entorno, portal, sobre)
    assert respuesta.json()["estado"] == "POR_VENCER"


def test_n2_4_el_mismo_evento_dos_veces_deja_una_sola_fila(
    entorno: Entorno, portal: TestClient
) -> None:
    sobre = _evento(HOY - timedelta(days=1), HOY + timedelta(days=364))
    poliza_id = sobre["payload"]["polizaId"]
    entorno.publicar(sobre)
    entorno.publicar(sobre)
    _esperar(
        lambda: portal.get(f"/v1/polizas/{poliza_id}/estado", headers=ASESOR).status_code == 200,
        "primera proyección",
    )
    _esperar(lambda: entorno.mensajes_en_cola() == 0, "cola vacía (ambos mensajes procesados)")
    filas = entorno.filas("SELECT count(*) FROM poliza_cliente WHERE poliza_id = %s", (poliza_id,))
    assert filas == [(1,)]


def test_n2_5_el_listado_incluye_la_poliza_y_actualizado_en_avanza(
    entorno: Entorno, portal: TestClient
) -> None:
    antes = portal.get("/v1/polizas", headers=ASESOR).json()
    sobre = _evento(HOY - timedelta(days=2), HOY + timedelta(days=363))
    _proyectar(entorno, portal, sobre)

    despues = portal.get(
        "/v1/polizas", params={"numero": sobre["payload"]["numero"]}, headers=ASESOR
    )
    cuerpo = despues.json()
    assert [i["polizaId"] for i in cuerpo["items"]] == [sobre["payload"]["polizaId"]]
    assert all("clienteId" not in i for i in cuerpo["items"])
    total = portal.get("/v1/polizas", headers=ASESOR).json()
    assert total["total"] == antes["total"] + 1
    assert datetime.fromisoformat(total["actualizadoEn"]) > datetime.fromisoformat(
        antes["actualizadoEn"]
    )


def test_n2_6_otro_grupo_es_403_antes_de_consultar(entorno: Entorno, portal: TestClient) -> None:
    respuesta = portal.get(f"/v1/polizas/{uuid4()}/estado", headers=SUSCRIPTOR)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (403, "ROL_NO_AUTORIZADO")


def test_n2_7_con_consulta_detenido_el_bff_responde_503(
    entorno: Entorno, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = SettingsConsulta()
    config.sqs_consulta_proyeccion_url = None
    with _servir(crear_app(config)) as url, _portal(url, monkeypatch) as portal:
        caliente = portal.get("/v1/polizas", headers=ASESOR)
        assert caliente.status_code == 200
    # Ya detenido (conexión rechazada): corte determinista, sin depender del arranque.
    with _portal_sin_calentar(url, monkeypatch) as portal:
        for destino in ("/v1/polizas", f"/v1/polizas/{uuid4()}/estado"):
            respuesta = portal.get(destino, headers=ASESOR)
            assert (respuesta.status_code, respuesta.json()["codigo"]) == (
                503,
                "SERVICIO_NO_DISPONIBLE",
            )


@contextmanager
def _portal_sin_calentar(url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(settings, "jwt_issuers", {"backoffice": BACKOFFICE})
    servicio = EstadoPolizas(ConsultaCliente(url, settings.consulta_timeout_ms))
    portal_app.dependency_overrides[ruta.estados] = lambda: servicio
    try:
        yield TestClient(portal_app)
    finally:
        portal_app.dependency_overrides.clear()
