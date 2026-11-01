"""N3 R3 (HU-W16), versión de módulo: de la aceptación en polizas al recaudo de la prima, sin
simular nada propio.

Corre en CI sin AWS. Cadena real de producción:
`polizas` (en proceso, con su relay) -> tópico de LocalStack 4.14.0 -> `dinero-aceptaciones` ->
`dinero` (en proceso: consumidor, relay y conciliación) -> `adaptador-pagos` (uvicorn) ->
proveedor de pagos simulado (WireMock con los mapeos versionados de `simulador-aliados`). El
tópico tiene las cuatro colas de infra/platform/catalog.tf con sus filtros; solo
`dinero-aceptaciones` tiene consumidor: las otras tres las lee la prueba. Las bases de los dos
servicios son dos bases del mismo PostgreSQL. Solo datos sintéticos.

- C01, aprobado: suscripción y aceptación de S01 por la API de polizas.
- C02 (rechazado), C06 (lento) y C07 (caído): solo S01 tiene una suscripción aprobada, así que su
  `OfertaAceptada` se inserta en el outbox de polizas con el escritor de producción (decisión de
  la 16.7); del relay en adelante, todo es real.
- Reintentos: con dinero detenido, el relay de polizas vuelve a publicar el mismo evento (se
  borra su marca, como si cayera entre publicar y marcar). Lo consume uno o dos dinero a la vez.
- Inspección (ADR 0013, decisión 6): ningún dato de tarjeta ni de cuenta fuera de adaptador-pagos
  en lo que produce cada caso.

El adaptador corta al proveedor a 1 s (C06 tarda 8 s); dinero espera al adaptador 6,5 s. La
conciliación se invoca con `conciliar_una_vez` y un reloj adelantado 10 min, como MT-W16-11: la
antigüedad mínima no baja de dos veces la visibilidad de la cola.
"""

import asyncio
import json
import logging
import re
import socket
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import boto3
import httpx
import psycopg
import pytest
import uvicorn
from adaptador_pagos.config import Settings as AjustesAdaptador
from adaptador_pagos.main import crear_app as crear_adaptador
from dinero.application.recaudo import conciliar, recaudar
from dinero.application.recaudo.conciliar import Conciliado, Resultado
from dinero.config import Settings
from dinero.main import armar_conciliacion, crear_app
from fastapi import FastAPI
from jsonschema import Draft202012Validator
from polizas.adapters.outbound.aceptaciones_pg import AceptacionesPg
from polizas.adapters.outbound.catalogo_http import CatalogoHttp
from polizas.adapters.outbound.codigos import CodigosPg
from polizas.adapters.outbound.cotizacion_http import CotizacionHttp
from polizas.adapters.outbound.decisiones_pg import DecisionesPg
from polizas.adapters.outbound.decisiones_pg import migrar as migrar_polizas
from polizas.adapters.outbound.eventos import (
    OFERTA_ACEPTADA,
    OfertaAceptadaV1,
    PrimaAceptadaV1,
    SobreEvento,
)
from polizas.adapters.outbound.outbox_pg import insertar
from polizas.config import Settings as AjustesPolizas
from polizas.domain.aceptacion.modelos import CanalAceptacion
from polizas.domain.suscripcion.insumos import huella
from polizas.main import Puertos
from polizas.main import crear_app as crear_polizas
from psycopg.rows import dict_row
from solventa_common.logging import FormateadorJson

docker = pytest.importorskip("docker")
pytestmark = pytest.mark.anyio

RAIZ = Path(__file__).resolve().parents[4]
SIMULADOR = RAIZ / "services" / "simulador-aliados"
EVENTOS = RAIZ / "contracts" / "events"
DATOS = json.loads(
    (RAIZ / "contracts/datos-sinteticos/cotizaciones-suscripcion.json").read_text(encoding="utf-8")
)
SEMILLA_REGLAS = (
    RAIZ
    / "services/catalogo/src/catalogo/adapters/outbound/semillas/reglas_suscripcion_2026_10_1.json"
)
S01 = next(c for c in DATOS["casos"] if c["id"] == "S01")["insumos"]
IMAGEN_WIREMOCK = "wiremock/wiremock:3.9.2"
REGION = "us-east-1"
# Las colas del tópico de eventos en infra/platform/catalog.tf: filtro y visibilidad (s). La DLQ
# (maxReceiveCount = 5) se replica en la única con consumidor aquí.
COLAS: dict[str, tuple[dict[str, Any] | None, int]] = {
    "dinero-aceptaciones": ({"eventType": ["OfertaAceptada"]}, 60),
    "polizas-recaudos": ({"eventType": [{"prefix": "Recaudo"}]}, 120),
    "avisos": ({"eventType": ["PolizaEmitida", "RecaudoConfirmado"]}, 60),
    "auditoria": (None, 60),
}
MAX_RECEPCIONES = 5
# Pagadores sintéticos (= clienteId) de mappings/pagos/ (16.7). Solo C01 es el de S01.
C01 = "00000000-0000-4000-8000-000000000001"  # aprobado
C02 = "00000000-0000-4000-8000-000000000002"  # rechazado (FONDOS_INSUFICIENTES)
C06 = "00000000-0000-4000-8000-000000000006"  # lento (8000 ms); la consulta, aprobado
C07 = "00000000-0000-4000-8000-000000000007"  # 503; la consulta, 404 cobro_no_encontrado
TIMEOUT_PROVEEDOR_MS = 1000
ASESOR = {"X-Actor-Id": "sub-asesor-1", "X-Actor-Rol": "ASESOR"}
VERSION_DECLARACIONES = "2026.10.1"
DECLARACIONES = {
    "leidoCondiciones": True,
    "informacionVeraz": True,
    "debitoAutomatico": True,
    "avisosElectronicos": True,
}

# --- Inspección de datos de tarjeta y de cuenta (las reglas de adaptador-pagos y del simulador) --
SUBCADENAS = (
    "tarjeta",
    "card",
    "cvv",
    "cvc",
    "cuenta",
    "account",
    "iban",
    "swift",
    "bancari",
    "banco",
    "bank",
    "expir",
    "vencimiento",
    "titular",
    "holder",
)
EXACTOS = {"pan", "pin", "nip", "numero", "number", "cbu", "clabe", "routing"}
# 13 a 19 dígitos seguidos (con espacios o guiones opcionales): forma de un número de tarjeta o de
# cuenta. Los uuid (pagadores e identificadores sintéticos) se quitan antes de buscar.
NUMERO_DE_TARJETA = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
UUID_TEXTO = re.compile(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", re.IGNORECASE)
# Lo que mide el reloj o identifica la traza (16.1b): no es un dato del pagador.
MEDIDOS = ("duration_ms", "trace_id", "span_id")


def _prohibido(nombre: str) -> bool:
    normalizado = re.sub(r"[^a-z0-9]", "", nombre.lower())
    return normalizado in EXACTOS or any(parte in normalizado for parte in SUBCADENAS)


def _claves(valor: Any) -> list[str]:
    if isinstance(valor, dict):
        return [c for k, v in valor.items() for c in (str(k), *_claves(v))]
    if isinstance(valor, list | tuple):
        return [c for v in valor for c in _claves(v)]
    return []


def hallazgos(origen: str, valor: Any) -> list[str]:
    """Claves con nombre de tarjeta o de cuenta y números con su forma, fuera de los uuid."""
    claves = [f"{origen}: clave {c}" for c in _claves(valor) if _prohibido(c)]
    texto = UUID_TEXTO.sub("", json.dumps(valor, ensure_ascii=False, default=str))
    return claves + [f"{origen}: número {n}" for n in NUMERO_DE_TARJETA.findall(texto)]


# --- Piezas del montaje ------------------------------------------------------------------------
class Servidor:
    """Una app ASGI servida por uvicorn en un hilo propio, como la R8 de bff-web.

    psycopg asíncrono no corre sobre el ProactorEventLoop de Windows: se usa un
    SelectorEventLoop propio (en Linux es el bucle por defecto).
    """

    def __init__(self, app: Any) -> None:
        self._zocalo = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._zocalo.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self._zocalo.getsockname()[1]}"
        self._servidor = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
        self._hilo = threading.Thread(target=self._correr, daemon=True)

    def _correr(self) -> None:
        asyncio.run(
            self._servidor.serve(sockets=[self._zocalo]), loop_factory=asyncio.SelectorEventLoop
        )

    @property
    def hilo(self) -> int | None:
        """El hilo del servidor: sus logs son los del adaptador."""
        return self._hilo.ident

    def arrancar(self) -> "Servidor":
        self._hilo.start()
        limite = time.monotonic() + 30
        while not self._servidor.started:
            assert self._hilo.is_alive(), "el servidor no arrancó (ver su log)"
            assert time.monotonic() < limite, "el servidor no arrancó a tiempo"
            time.sleep(0.05)
        return self

    def apagar(self) -> None:
        self._servidor.should_exit = True
        self._hilo.join(timeout=15)


class Simulador:
    """El proveedor de pagos simulado. Su journal cuenta los cobros y las consultas por
    `referencia` (= `claveRecaudo`)."""

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

    def limpiar_journal(self) -> None:
        self._admin.delete("/requests").raise_for_status()

    def _solicitudes(self) -> list[dict[str, Any]]:
        return [r["request"] for r in reversed(self._admin.get("/requests").json()["requests"])]

    def cobros(self, clave: str) -> list[dict[str, Any]]:
        """Los POST /pagos/v1/cobros de esa referencia, en orden de llegada."""
        return [
            s
            for s in self._solicitudes()
            if s["method"] == "POST"
            and s["url"] == "/pagos/v1/cobros"
            and json.loads(s["body"]).get("referencia") == clave
        ]

    def consultas(self, clave: str) -> list[dict[str, Any]]:
        """Los GET /pagos/v1/cobros?referencia= de esa referencia, en orden de llegada."""
        consultas = []
        for solicitud in self._solicitudes():
            partes = urlsplit(solicitud["url"])
            if (
                solicitud["method"] == "GET"
                and partes.path == "/pagos/v1/cobros"
                and parse_qs(partes.query).get("referencia") == [clave]
            ):
                consultas.append(solicitud)
        return consultas


class Proveedores:
    """Dobles HTTP de cotizacion y catalogo de polizas, como su arnés de módulo: los insumos
    sintéticos por `cotizacionId` y las reglas 2026.10.1 con la huella que publica catalogo."""

    def __init__(self) -> None:
        self.insumos = {c["insumos"]["cotizacionId"]: c["insumos"] for c in DATOS["casos"]}
        definicion = json.loads(SEMILLA_REGLAS.read_text(encoding="utf-8"))
        self.reglas = {
            "productoId": definicion["productoId"],
            "version": DATOS["reglas"]["version"],
            "vigenteDesde": "2026-10-01T00:00:00Z",
            "hash": huella(definicion),
            "definicion": definicion,
        }

    async def responder(self, pedido: httpx.Request) -> httpx.Response:
        if pedido.url.host == "catalogo":
            return httpx.Response(200, json=self.reglas)
        cotizacion = re.search(r"/v1/cotizaciones/([^/]+)/", pedido.url.path)
        insumos = self.insumos.get(cotizacion.group(1) if cotizacion else "")
        if insumos is None:
            return httpx.Response(404, json={"codigo": "COTIZACION_NO_ENCONTRADA"})
        return httpx.Response(200, json=insumos)


class RelojFijo:
    """El `ahoraDeReferencia` de los datos sintéticos: la oferta de S01 está vigente."""

    def ahora(self) -> datetime:
        return datetime.fromisoformat(DATOS["ahoraDeReferencia"])


class RelojAdelantado:
    """El reloj del sistema, 10 minutos en el futuro: lo recién creado ya es antiguo."""

    def ahora(self) -> datetime:
        return datetime.now(UTC) + timedelta(minutes=10)


class Captura(logging.Handler):
    """Los logs de la prueba en el formato de producción, formateados al emitirse (la correlación
    es la del contexto del registro), con el hilo que los emitió."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.setFormatter(FormateadorJson("r3-w16", "pruebas"))
        self.lineas: list[tuple[int | None, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lineas.append((record.thread, self.format(record)))

    def instalar(self) -> None:
        """`instrumentar` deja un solo handler a stdout al crear cada app: se vuelve a poner este."""
        raiz = logging.getLogger()
        raiz.handlers = [self]
        raiz.setLevel(logging.INFO)

    def registros(self, sin_hilo: int | None) -> list[dict[str, Any]]:
        return [json.loads(linea) for hilo, linea in self.lineas if hilo != sin_hilo]


def filas(url: str, sql: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conexion:
        return conexion.execute(sql).fetchall()


def filas_dict(url: str, sql: str) -> list[dict[str, Any]]:
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conexion:
        return conexion.execute(sql).fetchall()


async def esperar(condicion: Callable[[], Any], motivo: str, limite_s: float = 30) -> Any:
    fin = time.monotonic() + limite_s
    while not (valor := condicion()):
        assert time.monotonic() < fin, motivo
        await asyncio.sleep(0.1)
    return valor


def validador(tipo: str) -> Draft202012Validator:
    esquema = json.loads((EVENTOS / f"{tipo}.v1.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(esquema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def tipo_de(mensaje: dict[str, Any]) -> str:
    tipo: str = mensaje["MessageAttributes"]["eventType"]["StringValue"]
    return tipo


def sobre_de(mensaje: dict[str, Any]) -> dict[str, Any]:
    """El sobre en crudo (RawMessageDelivery), validado contra su esquema y con `type` igual al
    atributo `eventType`."""
    sobre: dict[str, Any] = json.loads(mensaje["Body"])
    assert sobre["type"] == tipo_de(mensaje)
    assert list(validador(sobre["type"]).iter_errors(sobre)) == []
    return sobre


class Cadena:
    """El montaje de una prueba: un tópico nuevo con las cuatro colas, las bases limpias y el
    journal del simulador vacío. `polizas()` y `dinero()` encienden cada servicio."""

    def __init__(
        self,
        *,
        url_polizas: str,
        url_dinero: str,
        localstack: str,
        adaptador: Servidor,
        simulador: Simulador,
    ) -> None:
        self.url_polizas = url_polizas
        self.url_dinero = url_dinero
        self.localstack = localstack
        self.adaptador = adaptador
        self.simulador = simulador
        self.captura = Captura()
        self.sns = boto3.client("sns", region_name=REGION, endpoint_url=localstack)
        self.sqs = boto3.client("sqs", region_name=REGION, endpoint_url=localstack)
        sufijo = uuid4().hex[:8]
        self.topico: str = self.sns.create_topic(Name=f"eventos-{sufijo}")["TopicArn"]
        self.dlq: str = self.sqs.create_queue(QueueName=f"dinero-aceptaciones-{sufijo}-dlq")[
            "QueueUrl"
        ]
        self.colas = {
            nombre: self._cola(
                f"{nombre}-{sufijo}", filtro, visibilidad, dlq=nombre == "dinero-aceptaciones"
            )
            for nombre, (filtro, visibilidad) in COLAS.items()
        }
        self.recibidos: dict[str, list[dict[str, Any]]] = {nombre: [] for nombre in COLAS}
        self.ajustes_dinero: Settings | None = None

    def _arn(self, url: str) -> str:
        arn: str = self.sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])[
            "Attributes"
        ]["QueueArn"]
        return arn

    def _cola(
        self, nombre: str, filtro: dict[str, Any] | None, visibilidad: int, *, dlq: bool
    ) -> str:
        atributos = {"VisibilityTimeout": str(visibilidad)}
        if dlq:
            atributos["RedrivePolicy"] = json.dumps(
                {"deadLetterTargetArn": self._arn(self.dlq), "maxReceiveCount": MAX_RECEPCIONES}
            )
        url: str = self.sqs.create_queue(QueueName=nombre, Attributes=atributos)["QueueUrl"]
        suscripcion = {"RawMessageDelivery": "true"}
        if filtro is not None:
            suscripcion["FilterPolicy"] = json.dumps(filtro)
        self.sns.subscribe(
            TopicArn=self.topico, Protocol="sqs", Endpoint=self._arn(url), Attributes=suscripcion
        )
        return url

    # --- Servicios -----------------------------------------------------------------------------
    @asynccontextmanager
    async def polizas(self) -> AsyncIterator[httpx.AsyncClient]:
        """polizas con su configuración real, el relay encendido y sus dobles de cotizacion y
        catalogo; devuelve un cliente de su API."""
        from psycopg_pool import AsyncConnectionPool

        reloj = RelojFijo()
        settings = AjustesPolizas(
            SOLVENTA_DATABASE_URL=self.url_polizas,
            OUTBOX_ENABLED=True,
            EVENTS_TOPIC_ARN=self.topico,
            AWS_REGION=REGION,
            AWS_ENDPOINT_URL=self.localstack,
            outbox_intervalo_s=0.1,
        )
        proveedores = Proveedores()
        async with (
            AsyncConnectionPool(self.url_polizas, min_size=1, max_size=10, open=False) as pool,
            httpx.AsyncClient(transport=httpx.MockTransport(proveedores.responder)) as salida,
        ):
            puertos = Puertos(
                insumos=CotizacionHttp(salida, "http://cotizacion", settings.cotizacion_timeout_ms),
                reglas=CatalogoHttp(salida, "http://catalogo", settings.catalogo_timeout_ms, reloj),
                decisiones=DecisionesPg(pool),
                codigos=CodigosPg(pool),
                reloj=reloj,
                aceptaciones=AceptacionesPg(pool),
            )
            app = crear_polizas(settings, puertos=puertos, pool=pool)
            self.captura.instalar()
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://polizas"
                ) as cliente,
            ):
                yield cliente

    @asynccontextmanager
    async def dinero(
        self, consumidores: int = 1, max_mensajes: int = 2
    ) -> AsyncIterator[list[FastAPI]]:
        """`consumidores` réplicas de dinero (cada una con su pool), con el consumidor, el relay y
        la conciliación de producción; arrancan juntas y compiten por la cola."""
        from psycopg_pool import AsyncConnectionPool

        settings = Settings(
            SQS_DINERO_ACEPTACIONES_URL=self.colas["dinero-aceptaciones"],
            ADAPTADOR_PAGOS_URL=self.adaptador.url,
            OUTBOX_ENABLED=True,
            EVENTS_TOPIC_ARN=self.topico,
            AWS_REGION=REGION,
            AWS_ENDPOINT_URL=self.localstack,
            consumidor_espera_larga_s=1,
            consumidor_max_mensajes=max_mensajes,
            outbox_intervalo_s=0.1,
        )
        self.ajustes_dinero = settings
        async with AsyncExitStack() as pila:
            apps = []
            for _ in range(consumidores):
                pool = await pila.enter_async_context(
                    AsyncConnectionPool(self.url_dinero, min_size=1, max_size=10, open=False)
                )
                apps.append(crear_app(settings, pool=pool))
                self.captura.instalar()
            for app in apps:
                await pila.enter_async_context(app.router.lifespan_context(app))
            yield apps

    async def conciliar(self, app: FastAPI) -> list[Conciliado]:
        """Un ciclo de la conciliación de producción, sobre el pool y el cliente del adaptador de
        la app, con el reloj adelantado."""
        assert self.ajustes_dinero is not None
        conciliacion = armar_conciliacion(
            app.state.pool, app.state.pagos, RelojAdelantado(), self.ajustes_dinero
        )
        return await conciliacion.conciliar_una_vez()

    # --- Entradas ------------------------------------------------------------------------------
    async def aceptar_s01(self, api: httpx.AsyncClient) -> tuple[dict[str, Any], str]:
        """Suscripción y aceptación de S01 por la API de polizas: la aceptación y su
        correlationId."""
        decision = await api.post(
            "/v1/suscripciones",
            json={"cotizacionId": S01["cotizacionId"]},
            headers={**ASESOR, "Idempotency-Key": str(uuid4())},
        )
        assert decision.status_code == 201, decision.json()
        cuerpo = decision.json()
        aceptacion = await api.post(
            f"/v1/cotizaciones/{cuerpo['cotizacionId']}/aceptacion",
            json={
                "decisionId": cuerpo["id"],
                "versionDeclaraciones": VERSION_DECLARACIONES,
                "declaraciones": DECLARACIONES,
            },
            headers={**ASESOR, "Idempotency-Key": str(uuid4())},
        )
        assert aceptacion.status_code == 201, aceptacion.json()
        datos: dict[str, Any] = aceptacion.json()
        return datos, aceptacion.headers["X-Correlation-Id"]

    async def aceptar_por_outbox(self, pagador: str) -> SobreEvento[OfertaAceptadaV1]:
        """Un `OfertaAceptada` del pagador, escrito en el outbox de polizas con su escritor de
        producción (la prima y la huella de S01): lo publica el relay."""
        ahora = datetime.now(UTC)
        clave = uuid4()
        sobre = SobreEvento[OfertaAceptadaV1](
            event_id=uuid4(),
            aggregate_id=clave,
            type=OFERTA_ACEPTADA,
            version=1,
            occurred_at=ahora,
            correlation_id=str(uuid4()),
            payload=OfertaAceptadaV1(
                aceptacion_id=clave,
                cotizacion_id=uuid4(),
                oferta_id=None,
                decision_id=uuid4(),
                cliente_id=UUID(pagador),
                producto_id=S01["productoId"],
                prima=PrimaAceptadaV1(
                    valor=S01["primaTotal"], moneda=S01["moneda"], periodicidad=S01["periodicidad"]
                ),
                clave_recaudo=clave,
                huella_calculo=S01["huellaCalculo"],
                vigente_hasta=ahora + timedelta(days=30),
                version_declaraciones=VERSION_DECLARACIONES,
                aceptada_en=ahora,
                actor_id=ASESOR["X-Actor-Id"],
                canal=CanalAceptacion.PORTAL_ASESOR,
            ),
        )
        conexion = await psycopg.AsyncConnection.connect(self.url_polizas)
        async with conexion, conexion.transaction():
            await insertar(conexion, [sobre])
        return sobre

    def republicar_ofertas(self) -> None:
        """Borra la marca de los `OfertaAceptada`: el relay de polizas los vuelve a publicar con
        el mismo eventId, como si hubiera caído entre publicar y marcar."""
        with psycopg.connect(self.url_polizas, autocommit=True) as conexion:
            conexion.execute(
                "UPDATE outbox SET published_at = NULL WHERE event_type = %s", (OFERTA_ACEPTADA,)
            )

    # --- Lecturas ------------------------------------------------------------------------------
    def _recibir(self, url: str, cantidad: int, espera_s: float) -> list[dict[str, Any]]:
        mensajes: list[dict[str, Any]] = []
        limite = time.monotonic() + espera_s
        while len(mensajes) < cantidad and time.monotonic() < limite:
            respuesta = self.sqs.receive_message(
                QueueUrl=url,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=1,
                MessageAttributeNames=["All"],
            )
            for mensaje in respuesta.get("Messages", []):
                mensajes.append(mensaje)
                self.sqs.delete_message(QueueUrl=url, ReceiptHandle=mensaje["ReceiptHandle"])
        return mensajes

    async def recibir(self, cola: str, cantidad: int, espera_s: float = 30) -> list[dict[str, Any]]:
        """Recibe y borra exactamente `cantidad` mensajes de una cola sin consumidor."""
        mensajes = await asyncio.to_thread(self._recibir, self.colas[cola], cantidad, espera_s)
        self.recibidos[cola].extend(mensajes)
        assert len(mensajes) == cantidad, f"{cola}: {len(mensajes)} de {cantidad} mensajes"
        return mensajes

    async def vacia(self, cola: str) -> bool:
        respuesta = await asyncio.to_thread(
            self.sqs.receive_message,
            QueueUrl=self.colas[cola],
            WaitTimeSeconds=2,
            MessageAttributeNames=["All"],
        )
        mensajes = respuesta.get("Messages", [])
        self.recibidos[cola].extend(mensajes)
        return not mensajes

    def en_cola(self, url: str) -> int:
        """Visibles más en vuelo: 0 si todo se borró."""
        atributos = self.sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        return sum(int(valor) for valor in atributos.values())

    def movimientos(self) -> list[tuple[Any, ...]]:
        return filas(
            self.url_dinero,
            "SELECT clave_recaudo, estado, transaccion_id, motivo, ultimo_incierto, monto, "
            "correlation_id FROM recaudos",
        )

    def movimiento(self, clave: str) -> tuple[Any, ...] | None:
        return next((m for m in self.movimientos() if str(m[0]) == clave), None)

    def outbox_dinero(self) -> list[tuple[Any, ...]]:
        return filas(
            self.url_dinero, "SELECT event_type, payload, published_at FROM outbox ORDER BY 1"
        )

    def publicados_dinero(self) -> bool:
        eventos = self.outbox_dinero()
        return bool(eventos) and all(publicado is not None for *_, publicado in eventos)

    def ofertas_publicadas(self) -> list[tuple[Any, ...]]:
        return filas(
            self.url_polizas,
            "SELECT event_id, payload, correlation_id FROM outbox "
            f"WHERE event_type = '{OFERTA_ACEPTADA}' AND published_at IS NOT NULL",
        )

    def inbox(self) -> list[tuple[Any, ...]]:
        return filas(self.url_dinero, "SELECT event_id, event_type FROM inbox")

    def registros(self) -> list[dict[str, Any]]:
        """Los logs capturados de polizas y dinero (sin los del hilo de adaptador-pagos)."""
        return self.captura.registros(sin_hilo=self.adaptador.hilo)

    # --- Inspección (caso 6) -------------------------------------------------------------------
    def comprobar_sin_datos_de_pago(self) -> None:
        """Ningún nombre de campo ni número con forma de tarjeta o de cuenta en los logs de
        polizas y dinero, sus outbox, lo recibido en las colas y las tablas de dinero."""
        registros = self.registros()
        recibidos = [m for mensajes in self.recibidos.values() for m in mensajes]
        assert registros and recibidos, "la inspección no tendría nada que revisar"
        encontrados: list[str] = []
        for i, registro in enumerate(registros):
            sin_medidos = {k: v for k, v in registro.items() if k not in MEDIDOS}
            encontrados += hallazgos(f"log {i} ({registro['logger']})", sin_medidos)
        for servicio, url in (("polizas", self.url_polizas), ("dinero", self.url_dinero)):
            for (payload,) in filas(url, "SELECT payload FROM outbox"):
                encontrados += hallazgos(f"outbox de {servicio}", payload)
        for cola, mensajes in self.recibidos.items():
            for mensaje in mensajes:
                visible = {
                    "atributos": mensaje.get("MessageAttributes", {}),
                    "cuerpo": json.loads(mensaje["Body"]),
                }
                encontrados += hallazgos(f"cola {cola}", visible)
        for tabla in ("recaudos", "outbox", "inbox"):
            for fila in filas_dict(self.url_dinero, f"SELECT * FROM {tabla}"):
                encontrados += hallazgos(f"tabla {tabla}", fila)
        assert encontrados == []


# --- Fixtures ----------------------------------------------------------------------------------
def _ejecutar(url: str, sql: str) -> None:
    with psycopg.connect(url, autocommit=True) as conexion:
        conexion.execute(sql)


@pytest.fixture(scope="module")
def base_polizas(base: str) -> str:
    """La base de polizas, otra base en el PostgreSQL de dinero (las dos tienen `outbox`)."""
    _ejecutar(base, "CREATE DATABASE polizas")
    url = f"{base.rsplit('/', 1)[0]}/polizas"
    migrar_polizas(url)
    return url


@pytest.fixture(scope="module")
def simulador(base: str) -> Iterator[Simulador]:
    from testcontainers.core.container import DockerContainer

    contenedor = (
        DockerContainer(IMAGEN_WIREMOCK)
        .with_exposed_ports(8080)
        .with_volume_mapping(str(SIMULADOR / "mappings"), "/home/wiremock/mappings", "ro")
        .with_volume_mapping(str(SIMULADOR / "__files"), "/home/wiremock/__files", "ro")
        .with_command("--disable-banner")
    )
    with contenedor:
        servidor = Simulador(
            f"http://{contenedor.get_container_host_ip()}:{contenedor.get_exposed_port(8080)}"
        )
        servidor.esperar_listo()
        yield servidor


@pytest.fixture(scope="module")
def adaptador(simulador: Simulador) -> Iterator[Servidor]:
    """adaptador-pagos real hacia el simulador, con el timeout al proveedor de prueba (1 s)."""
    servidor = Servidor(
        crear_adaptador(
            AjustesAdaptador(
                ALLY_PAGOS_URL=f"{simulador.url}/pagos", ALLY_PAGOS_TIMEOUT_MS=TIMEOUT_PROVEEDOR_MS
            )
        )
    ).arrancar()
    yield servidor
    servidor.apagar()


@pytest.fixture
def cadena(
    limpia: str,
    base_polizas: str,
    localstack: str,
    credenciales: None,
    simulador: Simulador,
    adaptador: Servidor,
) -> Iterator[Cadena]:
    _ejecutar(base_polizas, "TRUNCATE polizas, aceptaciones, decisiones_suscripcion, outbox")
    simulador.limpiar_journal()
    raiz = logging.getLogger()
    anteriores, nivel = list(raiz.handlers), raiz.level
    yield Cadena(
        url_polizas=base_polizas,
        url_dinero=limpia,
        localstack=localstack,
        adaptador=adaptador,
        simulador=simulador,
    )
    raiz.handlers, raiz.level = anteriores, nivel


# --- Casos -------------------------------------------------------------------------------------
async def test_c01_aprobado_de_la_aceptacion_en_polizas_al_recaudo_confirmado(
    cadena: Cadena,
) -> None:
    """C01: suscripción y aceptación por la API de polizas → su relay publica `OfertaAceptada` →
    dinero cobra por adaptador-pagos (APROBADO) → `CONFIRMADO` → su relay publica
    `RecaudoConfirmado` → lo reciben polizas-recaudos y avisos."""
    async with cadena.polizas() as api, cadena.dinero():
        aceptacion, correlacion = await cadena.aceptar_s01(api)
        clave = aceptacion["id"]
        [recaudo] = await cadena.recibir("polizas-recaudos", 1)
        [aviso] = await cadena.recibir("avisos", 1)
        auditoria = await cadena.recibir("auditoria", 3)
        await esperar(cadena.publicados_dinero, "el relay de dinero no marcó published_at")
        await esperar(
            lambda: cadena.en_cola(cadena.colas["dinero-aceptaciones"]) == 0,
            "dinero no borró el mensaje",
        )
        assert await cadena.vacia("polizas-recaudos") and await cadena.vacia("avisos")

    # polizas: la aceptación salió por su relay, con la prima de la decisión.
    [(event_id, oferta, correlacion_oferta)] = cadena.ofertas_publicadas()
    assert (oferta["claveRecaudo"], oferta["clienteId"], correlacion_oferta) == (
        clave,
        C01,
        correlacion,
    )
    prima = Decimal(oferta["prima"]["valor"])
    # dinero-aceptaciones recibió solo OfertaAceptada: un solo mensaje procesado (el inbox), nada
    # pendiente ni en la DLQ (un evento de otro tipo no valida y no se borra; P16-4).
    assert cadena.inbox() == [(event_id, "OfertaAceptada")]
    assert cadena.en_cola(cadena.dlq) == 0
    # El cobro: uno, con la claveRecaudo como referencia e Idempotency-Key, al pagador sintético.
    [cobro] = cadena.simulador.cobros(clave)
    assert cobro["headers"]["Idempotency-Key"] == clave
    cuerpo = json.loads(cobro["body"])
    assert (Decimal(cuerpo["monto"]), cuerpo["moneda"], cuerpo["pagador"]) == (prima, "COP", C01)
    [(clave_db, estado, transaccion, motivo, incierto, monto, correlacion_db)] = (
        cadena.movimientos()
    )
    assert (str(clave_db), estado, transaccion, motivo, incierto) == (
        clave,
        "CONFIRMADO",
        "TX-SINT-C01",
        None,
        None,
    )
    assert (monto, correlacion_db) == (prima, correlacion)
    [(tipo, _, _)] = cadena.outbox_dinero()
    assert tipo == "RecaudoConfirmado"
    # Los destinos: el mismo evento, validado, con la correlación de punta a punta y sin pagador.
    sobre = sobre_de(recaudo)
    assert sobre_de(aviso) == sobre
    assert sobre["type"] == "RecaudoConfirmado" and sobre["correlationId"] == correlacion
    assert sobre["payload"]["claveRecaudo"] == clave
    assert Decimal(sobre["payload"]["monto"]["valor"]) == prima
    assert sobre["payload"]["transaccionId"] == "TX-SINT-C01"
    assert C01 not in recaudo["Body"]
    assert sorted(tipo_de(m) for m in auditoria) == [
        "DecisionSuscripcionRegistrada",
        "OfertaAceptada",
        "RecaudoConfirmado",
    ]
    cadena.comprobar_sin_datos_de_pago()


async def test_c02_rechazado_publica_recaudo_rechazado_solo_hacia_polizas_recaudos(
    cadena: Cadena,
) -> None:
    async with cadena.polizas(), cadena.dinero():
        sobre_oferta = await cadena.aceptar_por_outbox(C02)
        clave = str(sobre_oferta.payload.clave_recaudo)
        [rechazo] = await cadena.recibir("polizas-recaudos", 1)
        auditoria = await cadena.recibir("auditoria", 2)
        await esperar(cadena.publicados_dinero, "el relay de dinero no marcó published_at")
        # avisos solo filtra PolizaEmitida y RecaudoConfirmado.
        assert await cadena.vacia("avisos")

    [(clave_db, estado, transaccion, motivo, incierto, *_)] = cadena.movimientos()
    assert (str(clave_db), estado, transaccion, motivo, incierto) == (
        clave,
        "RECHAZADO",
        None,
        "FONDOS_INSUFICIENTES",
        None,
    )
    sobre = sobre_de(rechazo)
    assert sobre["type"] == "RecaudoRechazado"
    assert sobre["correlationId"] == sobre_oferta.correlation_id
    assert (sobre["payload"]["claveRecaudo"], sobre["payload"]["motivo"]) == (
        clave,
        "FONDOS_INSUFICIENTES",
    )
    assert [tipo for tipo, *_ in cadena.outbox_dinero()] == ["RecaudoRechazado"]
    assert sorted(tipo_de(m) for m in auditoria) == ["OfertaAceptada", "RecaudoRechazado"]
    assert len(cadena.simulador.cobros(clave)) == 1
    cadena.comprobar_sin_datos_de_pago()


async def test_c06_lento_queda_pendiente_y_la_conciliacion_lo_confirma_con_sus_trazas(
    cadena: Cadena,
) -> None:
    """C06: el proveedor tarda 8 s y el adaptador corta a 1 s (504 TIEMPO_AGOTADO): PENDIENTE, sin
    evento. La conciliación consulta (APROBADO: el cobro sí ocurrió) y confirma, sin reintento."""
    async with cadena.polizas(), cadena.dinero() as [app]:
        sobre_oferta = await cadena.aceptar_por_outbox(C06)
        clave = str(sobre_oferta.payload.clave_recaudo)
        movimiento = await esperar(
            lambda: (m := cadena.movimiento(clave)) and m[4] and m, "el cobro no terminó"
        )
        assert movimiento[1:5] == ("PENDIENTE", None, None, "TIEMPO_AGOTADO")
        assert cadena.outbox_dinero() == []
        assert await cadena.vacia("polizas-recaudos") and await cadena.vacia("avisos")

        [conciliado] = await cadena.conciliar(app)
        assert (conciliado.resultado, conciliado.reintento) == (Resultado.CONFIRMADO, False)
        [confirmado] = await cadena.recibir("polizas-recaudos", 1)
        [aviso] = await cadena.recibir("avisos", 1)
        auditoria = await cadena.recibir("auditoria", 2)
        await esperar(cadena.publicados_dinero, "el relay de dinero no marcó published_at")

    assert cadena.movimiento(clave)[1:4] == ("CONFIRMADO", "TX-SINT-C06", None)
    sobre = sobre_de(confirmado)
    assert sobre_de(aviso) == sobre
    assert (sobre["type"], sobre["payload"]["claveRecaudo"]) == ("RecaudoConfirmado", clave)
    assert sobre["correlationId"] == sobre_oferta.correlation_id
    assert sorted(tipo_de(m) for m in auditoria) == ["OfertaAceptada", "RecaudoConfirmado"]
    assert len(cadena.simulador.cobros(clave)) == 1
    assert len(cadena.simulador.consultas(clave)) == 1
    # Las trazas: el incierto del consumidor y la conciliación, con la correlación del evento.
    registros = cadena.registros()
    [incierto] = [
        r
        for r in registros
        if r["message"] == recaudar.MENSAJE_INCIERTO and r.get("clave_recaudo") == clave
    ]
    assert (incierto["codigo"], incierto["correlation_id"]) == (
        "TIEMPO_AGOTADO",
        sobre_oferta.correlation_id,
    )
    [traza] = [
        r
        for r in registros
        if r["message"] == conciliar.MENSAJE_CONCILIADO and r.get("clave_recaudo") == clave
    ]
    assert (traza["resultado"], traza["reintento"], traza["correlation_id"]) == (
        "confirmado",
        False,
        sobre_oferta.correlation_id,
    )
    medicion = [
        r["result"] for r in registros if r["operation"] == "recaudo.conciliar" and r["result"]
    ]
    assert medicion == ["confirmado"]
    cadena.comprobar_sin_datos_de_pago()


async def test_c07_caido_sigue_pendiente_tras_conciliar_con_reintento_y_sin_evento(
    cadena: Cadena,
) -> None:
    """C07: el proveedor responde 503 (FUENTE_NO_DISPONIBLE): PENDIENTE. La consulta responde 404
    cobro_no_encontrado: se reintenta el cobro con la misma clave (P16-3), otra vez 503: sigue
    PENDIENTE y sin evento. Nunca se asume rechazo."""
    async with cadena.polizas(), cadena.dinero() as [app]:
        sobre_oferta = await cadena.aceptar_por_outbox(C07)
        clave = str(sobre_oferta.payload.clave_recaudo)
        movimiento = await esperar(
            lambda: (m := cadena.movimiento(clave)) and m[4] and m, "el cobro no terminó"
        )
        assert movimiento[1:5] == ("PENDIENTE", None, None, "FUENTE_NO_DISPONIBLE")

        [conciliado] = await cadena.conciliar(app)
        assert (conciliado.resultado, conciliado.codigo, conciliado.reintento) == (
            Resultado.INCIERTO,
            "FUENTE_NO_DISPONIBLE",
            True,
        )
        auditoria = await cadena.recibir("auditoria", 1)
        assert await cadena.vacia("polizas-recaudos") and await cadena.vacia("avisos")

    assert cadena.movimiento(clave)[1:5] == ("PENDIENTE", None, None, "FUENTE_NO_DISPONIBLE")
    assert cadena.outbox_dinero() == []
    assert [tipo_de(m) for m in auditoria] == ["OfertaAceptada"]
    cobros = cadena.simulador.cobros(clave)
    assert [c["headers"]["Idempotency-Key"] for c in cobros] == [clave, clave]
    assert len(cadena.simulador.consultas(clave)) == 1
    [traza] = [
        r
        for r in cadena.registros()
        if r["message"] == conciliar.MENSAJE_CONCILIADO and r.get("clave_recaudo") == clave
    ]
    assert (traza["resultado"], traza["codigo"], traza["reintento"]) == (
        "incierto",
        "FUENTE_NO_DISPONIBLE",
        True,
    )
    cadena.comprobar_sin_datos_de_pago()


async def _publicar_de_nuevo(cadena: Cadena, en_cola: int) -> None:
    """Con dinero detenido: el relay de polizas vuelve a publicar los `OfertaAceptada` y la cola
    llega a `en_cola` mensajes."""
    cadena.republicar_ofertas()
    await esperar(
        lambda: cadena.en_cola(cadena.colas["dinero-aceptaciones"]) == en_cola,
        f"dinero-aceptaciones no llegó a {en_cola} mensajes",
    )


async def test_el_mismo_oferta_aceptada_publicado_dos_veces_cobra_una_sola_vez(
    cadena: Cadena,
) -> None:
    """Una reentrega del mismo eventId después de confirmar: el inbox la descarta y no hay otro
    cobro en el journal del simulador."""
    async with cadena.polizas() as api:
        async with cadena.dinero():
            aceptacion, _ = await cadena.aceptar_s01(api)
            clave = aceptacion["id"]
            await cadena.recibir("polizas-recaudos", 1)
            await esperar(cadena.publicados_dinero, "el relay de dinero no marcó published_at")
        await _publicar_de_nuevo(cadena, en_cola=1)
        async with cadena.dinero():
            await esperar(
                lambda: cadena.en_cola(cadena.colas["dinero-aceptaciones"]) == 0,
                "dinero no consumió la reentrega",
            )
            assert await cadena.vacia("polizas-recaudos")
        await cadena.recibir("auditoria", 4)

    [(event_id, *_)] = cadena.ofertas_publicadas()
    assert cadena.inbox() == [(event_id, "OfertaAceptada")]
    assert len(cadena.simulador.cobros(clave)) == 1
    assert [(str(m[0]), m[1]) for m in cadena.movimientos()] == [(clave, "CONFIRMADO")]
    assert [tipo for tipo, *_ in cadena.outbox_dinero()] == ["RecaudoConfirmado"]
    assert cadena.en_cola(cadena.dlq) == 0
    cadena.comprobar_sin_datos_de_pago()


async def test_dos_dinero_concurrentes_con_el_mismo_oferta_aceptada_cobran_una_sola_vez(
    cadena: Cadena,
) -> None:
    """Las dos copias del mismo eventId esperan en la cola; dos réplicas de dinero arrancan a la
    vez y toman una cada una (un mensaje por recepción): un solo cobro, un movimiento y un
    evento."""
    async with cadena.polizas() as api:
        aceptacion, _ = await cadena.aceptar_s01(api)
        clave = aceptacion["id"]
        await esperar(cadena.ofertas_publicadas, "el relay de polizas no publicó la aceptación")
        await _publicar_de_nuevo(cadena, en_cola=2)
        async with cadena.dinero(consumidores=2, max_mensajes=1):
            [recaudo] = await cadena.recibir("polizas-recaudos", 1)
            await esperar(
                lambda: cadena.en_cola(cadena.colas["dinero-aceptaciones"]) == 0,
                "dinero no consumió las dos copias",
            )
            await esperar(cadena.publicados_dinero, "el relay de dinero no marcó published_at")
            assert await cadena.vacia("polizas-recaudos")
        await cadena.recibir("auditoria", 4)

    assert sobre_de(recaudo)["payload"]["claveRecaudo"] == clave
    [(event_id, *_)] = cadena.ofertas_publicadas()
    assert cadena.inbox() == [(event_id, "OfertaAceptada")]
    assert len(cadena.simulador.cobros(clave)) == 1
    assert [(str(m[0]), m[1]) for m in cadena.movimientos()] == [(clave, "CONFIRMADO")]
    assert [tipo for tipo, *_ in cadena.outbox_dinero()] == ["RecaudoConfirmado"]
    assert cadena.en_cola(cadena.dlq) == 0
    cadena.comprobar_sin_datos_de_pago()


def _propiedades(nodo: Any) -> Iterator[str]:
    if isinstance(nodo, dict):
        for clave, valor in nodo.items():
            if clave in ("properties", "$defs") and isinstance(valor, dict):
                yield from valor
            yield from _propiedades(valor)
    elif isinstance(nodo, list):
        for elemento in nodo:
            yield from _propiedades(elemento)


async def test_inspeccion_esquemas_de_eventos_y_columnas_de_dinero_sin_datos_de_pago(
    limpia: str,
) -> None:
    """Lo estático de la inspección: los esquemas publicados de los dos eventos nuevos y todas las
    columnas de las tablas de dinero (los casos revisan las filas que dejan)."""
    for tipo in ("RecaudoConfirmado", "RecaudoRechazado"):
        esquema = json.loads((EVENTOS / f"{tipo}.v1.schema.json").read_text(encoding="utf-8"))
        nombres = list(_propiedades(esquema))
        assert "claveRecaudo" in nombres
        assert [n for n in nombres if _prohibido(n)] == [], tipo
        assert hallazgos(tipo, esquema) == []
    columnas = filas(
        limpia,
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'public'",
    )
    assert {tabla for tabla, _ in columnas} >= {"recaudos", "outbox", "inbox"}
    assert [(t, c) for t, c in columnas if _prohibido(t) or _prohibido(c)] == []
