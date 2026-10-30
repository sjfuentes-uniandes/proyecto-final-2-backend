"""N3 R3 (HU-W17), versión de módulo: de la aceptación en polizas a la póliza ACTIVA, con su
documento verificable y `PolizaEmitida` en las colas, sin simular nada propio.

Corre en CI sin AWS. Es la cadena de la R3 de W16 (`services/dinero/tests/module/
test_r3_recaudo_w16.py`) más la emisión, toda de producción:
`polizas` (en proceso: API, relay, consumidor de `polizas-recaudos` y tarea emisora) -> tópico de
LocalStack 4.14.0 -> `dinero-aceptaciones` -> `dinero` (en proceso: consumidor, relay y
conciliación) -> `adaptador-pagos` (uvicorn) -> pagos simulado; `RecaudoConfirmado` ->
`polizas-recaudos` -> polizas (EMITIENDO) -> PDF en un bucket versionado del mismo LocalStack
(`S3_SKIP_SIGNATURE_VALIDATION=0`) -> `adaptador-firma` (uvicorn) -> firma simulada -> ACTIVA y
`PolizaEmitida`. Los dos proveedores son un WireMock 3.9.2 con los mapeos versionados de
`simulador-aliados`. El tópico tiene las cinco colas de infra/platform/catalog.tf con sus filtros,
su visibilidad y su DLQ (maxReceiveCount = 5); `consulta-proyeccion`, `avisos` y `auditoria` las
lee la prueba. polizas y dinero usan dos bases del mismo PostgreSQL. El reloj de polizas es el
`ahoraDeReferencia` de los datos sintéticos: la oferta de S01 ya venció. Solo datos sintéticos.

- C01: suscripción y aceptación de S01 por la API → ACTIVA, `PolizaEmitida` en las tres colas, la
  póliza por la API, el PDF por su URL y su verificación; la misma correlación de punta a punta.
- AC3: un consumidor de prueba de `avisos` no borra `PolizaEmitida` (el aviso falló) y lo vuelve a
  recibir igual; entre tanto `RecaudoConfirmado` se reentrega y la tarea emisora corre otra vez.
- C06, C07 y C02: solo S01 tiene una suscripción aprobada, así que su decisión se copia con el
  cliente (= pagador y firmante sintético) y otros identificadores, con el escritor de producción
  (`DecisionesPg`), y se acepta por la API.
  - C06: el cobro es lento (la conciliación lo confirma, como en W16) y la firma también.
  - C07: el simulador de pagos nunca confirma a C07 (cobro 503 y consulta 404; R3 de W16), así que
    dinero no lo consume y su `RecaudoConfirmado` se escribe en el outbox de dinero con su escritor
    de producción (`OutboxRecaudosPg`); del relay de dinero en adelante, todo es real.
  - C02: el cobro se rechaza.
- Concurrencia: dos tareas emisoras sobre la misma póliza nueva.

C01, AC3 y C02 corren la tarea emisora como tarea de la app; C06, C07 y la concurrencia la apagan y
la invocan con `emitir_una_vez`, para fijar cuándo corre cada intento. adaptador-pagos y
adaptador-firma cortan al proveedor a 1 s (C06 tarda 8 s en los dos); polizas espera a
adaptador-firma 6,5 s.
"""

import asyncio
import hashlib
import json
import logging
import random
import re
import socket
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import replace
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
from adaptador_firma.config import Settings as AjustesFirma
from adaptador_firma.main import crear_app as crear_adaptador_firma
from adaptador_pagos.config import Settings as AjustesPagos
from adaptador_pagos.main import crear_app as crear_adaptador_pagos
from dinero.adapters.outbound.outbox_pg import OutboxRecaudosPg
from dinero.adapters.outbound.recaudos_pg import TransaccionPg
from dinero.adapters.outbound.recaudos_pg import migrar as migrar_dinero
from dinero.config import Settings as AjustesDinero
from dinero.domain.recaudo.modelos import EstadoRecaudo, Moneda, Recaudo
from dinero.main import armar_conciliacion
from dinero.main import crear_app as crear_dinero
from fastapi import FastAPI
from jsonschema import Draft202012Validator
from polizas.adapters.outbound.aceptaciones_pg import AceptacionesPg
from polizas.adapters.outbound.catalogo_http import CatalogoHttp
from polizas.adapters.outbound.codigos import CodigosPg
from polizas.adapters.outbound.cotizacion_http import CotizacionHttp
from polizas.adapters.outbound.decisiones_pg import DecisionesPg
from polizas.adapters.outbound.documentos_s3 import AlmacenDocumentosS3, cliente_s3
from polizas.adapters.outbound.firma_http import AdaptadorFirmaHttp
from polizas.adapters.outbound.polizas_pg import PolizasLecturaPg
from polizas.adapters.outbound.recaudos_pg import RecaudosPg
from polizas.application.poliza.emitir import EmitirPolizas, Resultado
from polizas.config import Settings as AjustesPolizas
from polizas.main import Puertos, armar_emisora
from polizas.main import crear_app as crear_polizas
from psycopg.rows import dict_row
from solventa_common import correlation
from solventa_common.logging import FormateadorJson

docker = pytest.importorskip("docker")
pytestmark = pytest.mark.anyio

RAIZ = Path(__file__).resolve().parents[4]
SIMULADOR = RAIZ / "services" / "simulador-aliados"
EVENTOS = RAIZ / "contracts" / "events"
IMAGEN_WIREMOCK = "wiremock/wiremock:3.9.2"
REGION = "us-east-1"
# Las cinco colas del tópico de eventos en infra/platform/catalog.tf: filtro y visibilidad (s).
# Todas con su DLQ y maxReceiveCount = 5, como infra.
COLAS: dict[str, tuple[dict[str, Any] | None, int]] = {
    "dinero-aceptaciones": ({"eventType": ["OfertaAceptada"]}, 60),
    "polizas-recaudos": ({"eventType": [{"prefix": "Recaudo"}]}, 120),
    "consulta-proyeccion": ({"eventType": [{"prefix": "Poliza"}, {"prefix": "Siniestro"}]}, 30),
    "avisos": ({"eventType": ["PolizaEmitida", "RecaudoConfirmado"]}, 60),
    "auditoria": (None, 60),
}
MAX_RECEPCIONES = 5
# Clientes sintéticos (= pagador y firmante) de mappings/pagos/ y mappings/firma/. Solo C01 es el
# de S01.
C01 = "00000000-0000-4000-8000-000000000001"  # cobro aprobado; firma en 50 ms
C02 = "00000000-0000-4000-8000-000000000002"  # cobro rechazado (FONDOS_INSUFICIENTES)
C06 = "00000000-0000-4000-8000-000000000006"  # cobro y firma lentos (8000 ms)
C07 = "00000000-0000-4000-8000-000000000007"  # cobro y firma caídos (503)
TIMEOUT_PROVEEDOR_MS = 1000
ASESOR = {"X-Actor-Id": "sub-asesor-1", "X-Actor-Rol": "ASESOR"}
VERSION_DECLARACIONES = "2026.10.1"
DECLARACIONES = {
    "leidoCondiciones": True,
    "informacionVeraz": True,
    "debitoAutomatico": True,
    "avisosElectronicos": True,
}
VERIFICACIONES = "/v1/polizas/verificaciones"
# El consumidor de prueba de avisos: cuánto tarda en reaparecer lo que no borra.
VISIBILIDAD_AVISO_S = 5


# --- Piezas del montaje ------------------------------------------------------------------------
class Servidor:
    """Una app ASGI servida por uvicorn en un hilo propio, como la R3 de W16.

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
    """Los proveedores de pagos y de firma simulados. Su journal cuenta los cobros por
    `referencia` (= `claveRecaudo`) y las firmas por `documentoId` (= `polizaId`)."""

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

    def agregar(self, mapeo: dict[str, Any]) -> str:
        """Un mapeo en tiempo de ejecución (en memoria: no toca los archivos); devuelve su id."""
        respuesta = self._admin.post("/mappings", json=mapeo)
        respuesta.raise_for_status()
        identificador: str = respuesta.json()["id"]
        return identificador

    def quitar(self, identificador: str) -> None:
        self._admin.delete(f"/mappings/{identificador}").raise_for_status()

    def _solicitudes(self, metodo: str, ruta: str) -> list[dict[str, Any]]:
        solicitudes = self._admin.get("/requests").json()["requests"]
        return [
            s["request"]
            for s in reversed(solicitudes)
            if s["request"]["method"] == metodo and s["request"]["url"] == ruta
        ]

    def cobros(self, clave: str) -> list[dict[str, Any]]:
        """Los POST /pagos/v1/cobros de esa referencia, en orden de llegada."""
        return [
            s
            for s in self._solicitudes("POST", "/pagos/v1/cobros")
            if json.loads(s["body"]).get("referencia") == clave
        ]

    def firmas(self, poliza_id: str | None = None) -> list[dict[str, Any]]:
        """Los POST /firma/v1/firmas (de esa póliza, si se da), en orden de llegada."""
        return [
            s
            for s in self._solicitudes("POST", "/firma/v1/firmas")
            if poliza_id is None or json.loads(s["body"]).get("documentoId") == poliza_id
        ]


class Proveedores:
    """Dobles HTTP de cotizacion y catalogo de polizas, como su arnés de módulo: los insumos
    sintéticos por `cotizacionId` y las reglas que publica catalogo."""

    def __init__(self, sinteticos: Any) -> None:
        self.insumos = {
            c["insumos"]["cotizacionId"]: c["insumos"] for c in sinteticos.datos["casos"]
        }
        self.reglas = sinteticos.respuesta_reglas()

    async def responder(self, pedido: httpx.Request) -> httpx.Response:
        if pedido.url.host == "catalogo":
            return httpx.Response(200, json=self.reglas)
        cotizacion = re.search(r"/v1/cotizaciones/([^/]+)/", pedido.url.path)
        insumos = self.insumos.get(cotizacion.group(1) if cotizacion else "")
        if insumos is None:
            return httpx.Response(404, json={"codigo": "COTIZACION_NO_ENCONTRADA"})
        return httpx.Response(200, json=insumos)


class RelojAdelantado:
    """El reloj del sistema, 10 minutos en el futuro: lo recién creado ya es antiguo (para la
    conciliación de dinero, como MT-W16-11)."""

    def ahora(self) -> datetime:
        return datetime.now(UTC) + timedelta(minutes=10)


class Captura(logging.Handler):
    """Los logs de la prueba en el formato de producción, formateados al emitirse (la correlación
    es la del contexto del registro)."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.setFormatter(FormateadorJson("r3-w17", "pruebas"))
        self.lineas: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lineas.append(self.format(record))

    def instalar(self) -> None:
        """`instrumentar` deja un solo handler a stdout al crear cada app: se vuelve a poner este."""
        raiz = logging.getLogger()
        raiz.handlers = [self]
        raiz.setLevel(logging.INFO)

    def registros(self) -> list[dict[str, Any]]:
        return [json.loads(linea) for linea in list(self.lineas)]


def filas(url: str, sql: str, parametros: Any = None) -> list[dict[str, Any]]:
    with psycopg.connect(url, autocommit=True) as conexion:
        return conexion.cursor(row_factory=dict_row).execute(sql, parametros).fetchall()


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
    """El sobre en crudo (RawMessageDelivery), validado contra su esquema publicado y con `type`
    igual al atributo `eventType`."""
    sobre: dict[str, Any] = json.loads(mensaje["Body"])
    assert sobre["type"] == tipo_de(mensaje)
    assert [e.message for e in validador(sobre["type"]).iter_errors(sobre)] == []
    return sobre


def sha256(contenido: bytes) -> str:
    return "sha256:" + hashlib.sha256(contenido).hexdigest()


class Cadena:
    """El montaje de una prueba: un tópico nuevo con las cinco colas, las bases limpias, el
    bucket nuevo y el journal del simulador vacío. `polizas()` y `dinero()` encienden cada
    servicio; `emisora()` arma otra tarea emisora de producción."""

    def __init__(
        self,
        *,
        url_polizas: str,
        url_dinero: str,
        localstack: str,
        bucket: str,
        pagos: Servidor,
        firma: Servidor,
        simulador: Simulador,
        reloj: Any,
        sinteticos: Any,
    ) -> None:
        self.url_polizas = url_polizas
        self.url_dinero = url_dinero
        self.localstack = localstack
        self.bucket = bucket
        self.pagos = pagos
        self.firma = firma
        self.simulador = simulador
        self.reloj = reloj
        self.sinteticos = sinteticos
        self.captura = Captura()
        self.sns = boto3.client("sns", region_name=REGION, endpoint_url=localstack)
        self.sqs = boto3.client("sqs", region_name=REGION, endpoint_url=localstack)
        self.s3 = boto3.client("s3", region_name=REGION, endpoint_url=localstack)
        sufijo = uuid4().hex[:8]
        self.topico: str = self.sns.create_topic(Name=f"eventos-{sufijo}")["TopicArn"]
        self.dlqs: dict[str, str] = {}
        self.colas = {
            nombre: self._cola(nombre, sufijo, filtro, visibilidad)
            for nombre, (filtro, visibilidad) in COLAS.items()
        }
        self.ajustes_dinero: AjustesDinero | None = None

    def _arn(self, url: str) -> str:
        arn: str = self.sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])[
            "Attributes"
        ]["QueueArn"]
        return arn

    def _cola(
        self, nombre: str, sufijo: str, filtro: dict[str, Any] | None, visibilidad: int
    ) -> str:
        dlq: str = self.sqs.create_queue(QueueName=f"{nombre}-{sufijo}-dlq")["QueueUrl"]
        self.dlqs[nombre] = dlq
        atributos = {
            "VisibilityTimeout": str(visibilidad),
            "RedrivePolicy": json.dumps(
                {"deadLetterTargetArn": self._arn(dlq), "maxReceiveCount": MAX_RECEPCIONES}
            ),
        }
        url: str = self.sqs.create_queue(QueueName=f"{nombre}-{sufijo}", Attributes=atributos)[
            "QueueUrl"
        ]
        suscripcion = {"RawMessageDelivery": "true"}
        if filtro is not None:
            suscripcion["FilterPolicy"] = json.dumps(filtro)
        self.sns.subscribe(
            TopicArn=self.topico, Protocol="sqs", Endpoint=self._arn(url), Attributes=suscripcion
        )
        return url

    # --- Servicios -----------------------------------------------------------------------------
    def ajustes_polizas(self, *, emisora: bool) -> AjustesPolizas:
        """La configuración real de polizas hacia este montaje, con ciclos cortos."""
        return AjustesPolizas(
            SOLVENTA_DATABASE_URL=self.url_polizas,
            OUTBOX_ENABLED=True,
            EVENTS_TOPIC_ARN=self.topico,
            AWS_REGION=REGION,
            AWS_ENDPOINT_URL=self.localstack,
            SQS_POLIZAS_RECAUDOS_URL=self.colas["polizas-recaudos"],
            POLIZAS_BUCKET=self.bucket,
            ADAPTADOR_FIRMA_URL=self.firma.url,
            consumidor_espera_larga_s=1,
            outbox_intervalo_s=0.1,
            emision_habilitada=emisora,
            emision_intervalo_s=0.1,
        )

    @asynccontextmanager
    async def polizas(self, *, emisora: bool = True) -> AsyncIterator[httpx.AsyncClient]:
        """polizas con su configuración real: relay, consumidor y, si se pide, la tarea emisora;
        con sus dobles de cotizacion y catalogo. Devuelve un cliente de su API."""
        from psycopg_pool import AsyncConnectionPool

        settings = self.ajustes_polizas(emisora=emisora)
        proveedores = Proveedores(self.sinteticos)
        async with (
            AsyncConnectionPool(self.url_polizas, min_size=1, max_size=10, open=False) as pool,
            httpx.AsyncClient(transport=httpx.MockTransport(proveedores.responder)) as salida,
        ):
            puertos = Puertos(
                insumos=CotizacionHttp(salida, "http://cotizacion", settings.cotizacion_timeout_ms),
                reglas=CatalogoHttp(
                    salida, "http://catalogo", settings.catalogo_timeout_ms, self.reloj
                ),
                decisiones=DecisionesPg(pool),
                codigos=CodigosPg(pool),
                reloj=self.reloj,
                aceptaciones=AceptacionesPg(pool),
                recaudos=RecaudosPg(pool),
                polizas=PolizasLecturaPg(pool),
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
    async def dinero(self, *, consumidor: bool = True) -> AsyncIterator[FastAPI]:
        """dinero con el relay y la conciliación de producción y, si se pide, el consumidor de
        `dinero-aceptaciones` (sin la cola, no arranca: lo registra y la app sigue)."""
        from psycopg_pool import AsyncConnectionPool

        cola = {"SQS_DINERO_ACEPTACIONES_URL": self.colas["dinero-aceptaciones"]}
        settings = AjustesDinero(
            **(cola if consumidor else {}),
            ADAPTADOR_PAGOS_URL=self.pagos.url,
            OUTBOX_ENABLED=True,
            EVENTS_TOPIC_ARN=self.topico,
            AWS_REGION=REGION,
            AWS_ENDPOINT_URL=self.localstack,
            consumidor_espera_larga_s=1,
            outbox_intervalo_s=0.1,
        )
        self.ajustes_dinero = settings
        async with AsyncConnectionPool(
            self.url_dinero, min_size=1, max_size=10, open=False
        ) as pool:
            app = crear_dinero(settings, pool=pool)
            self.captura.instalar()
            async with app.router.lifespan_context(app):
                yield app

    async def conciliar(self, app: FastAPI) -> None:
        """Un ciclo de la conciliación de producción de dinero, con el reloj adelantado."""
        assert self.ajustes_dinero is not None
        conciliacion = armar_conciliacion(
            app.state.pool, app.state.pagos, RelojAdelantado(), self.ajustes_dinero
        )
        await conciliacion.conciliar_una_vez()

    @asynccontextmanager
    async def emisora(self) -> AsyncIterator[EmitirPolizas[Any]]:
        """Una tarea emisora de producción (`armar_emisora`) sobre su propio pool, con el bucket,
        adaptador-firma y el reloj de este montaje."""
        from psycopg_pool import AsyncConnectionPool

        settings = self.ajustes_polizas(emisora=True)
        almacen = AlmacenDocumentosS3(
            cliente_s3(REGION, endpoint_url=self.localstack),
            self.bucket,
            settings.polizas_bucket_sse,
        )
        async with (
            AsyncConnectionPool(self.url_polizas, min_size=1, max_size=4, open=False) as pool,
            correlation.cliente_async() as cliente,
        ):
            firma = AdaptadorFirmaHttp(cliente, self.firma.url, settings.adaptador_firma_timeout_ms)
            yield armar_emisora(pool, almacen, firma, self.reloj, settings)

    # --- Entradas ------------------------------------------------------------------------------
    async def _decidir_s01(self, api: httpx.AsyncClient) -> dict[str, Any]:
        decision = await api.post(
            "/v1/suscripciones",
            json={"cotizacionId": self.sinteticos.cotizacion_id("S01")},
            headers={**ASESOR, "Idempotency-Key": str(uuid4())},
        )
        assert decision.status_code == 201, decision.json()
        cuerpo: dict[str, Any] = decision.json()
        return cuerpo

    async def _aceptar(
        self, api: httpx.AsyncClient, cotizacion_id: str, decision_id: str
    ) -> tuple[dict[str, Any], str]:
        aceptacion = await api.post(
            f"/v1/cotizaciones/{cotizacion_id}/aceptacion",
            json={
                "decisionId": decision_id,
                "versionDeclaraciones": VERSION_DECLARACIONES,
                "declaraciones": DECLARACIONES,
            },
            headers={**ASESOR, "Idempotency-Key": str(uuid4())},
        )
        assert aceptacion.status_code == 201, aceptacion.json()
        datos: dict[str, Any] = aceptacion.json()
        return datos, aceptacion.headers["X-Correlation-Id"]

    async def aceptar_s01(self, api: httpx.AsyncClient) -> tuple[dict[str, Any], str]:
        """Suscripción y aceptación de S01 (C01) por la API de polizas: la aceptación y su
        correlationId."""
        decision = await self._decidir_s01(api)
        return await self._aceptar(api, decision["cotizacionId"], decision["id"])

    async def aceptar_como(
        self, api: httpx.AsyncClient, cliente: str
    ) -> tuple[dict[str, Any], str]:
        """La decisión APROBADA de S01 copiada con `cliente` y otros identificadores (escritor de
        producción), y su aceptación por la API: la aceptación y su correlationId."""
        from psycopg_pool import AsyncConnectionPool

        decision = await self._decidir_s01(api)
        async with AsyncConnectionPool(self.url_polizas, min_size=1, max_size=2, open=False) as p:
            decisiones = DecisionesPg(p)
            original = await decisiones.por_id(UUID(decision["id"]))
            assert original is not None
            copia = replace(
                original,
                id=uuid4(),
                codigo=f"DEC-2026-{random.randint(900_000, 999_999)}",
                cotizacion_id=uuid4(),
                cliente_id=UUID(cliente),
                idempotency_key=uuid4(),
            )
            assert await decisiones.guardar(copia)
        return await self._aceptar(api, str(copia.cotizacion_id), str(copia.id))

    async def confirmar_en_dinero(self, clave: str, pagador: str, correlacion: str) -> None:
        """Un `RecaudoConfirmado` de la clave en el outbox de dinero, con su escritor de producción
        y la prima del `OfertaAceptada`: lo publica el relay de dinero."""
        [oferta] = [o for o in self.ofertas() if o["payload"]["claveRecaudo"] == clave]
        prima = oferta["payload"]["prima"]
        ahora = datetime.now(UTC)
        recaudo = Recaudo(
            recaudo_id=uuid4(),
            clave_recaudo=UUID(clave),
            cotizacion_id=UUID(oferta["payload"]["cotizacionId"]),
            pagador_ref=UUID(pagador),
            monto=Decimal(prima["valor"]),
            moneda=Moneda(prima["moneda"]),
            periodicidad=prima["periodicidad"],
            estado=EstadoRecaudo.CONFIRMADO,
            transaccion_id="TX-SINT-C07",
            motivo=None,
            ultimo_incierto=None,
            creado_en=ahora,
            actualizado_en=ahora,
            resuelto_en=ahora,
            correlation_id=correlacion,
        )
        conexion = await psycopg.AsyncConnection.connect(self.url_dinero)
        async with conexion, conexion.transaction():
            await OutboxRecaudosPg().agregar(recaudo, TransaccionPg(conexion))

    def republicar_recaudos(self) -> None:
        """Borra la marca de los `Recaudo*` de dinero: su relay los vuelve a publicar con el mismo
        eventId, como si hubiera caído entre publicar y marcar."""
        with psycopg.connect(self.url_dinero, autocommit=True) as conexion:
            conexion.execute("UPDATE outbox SET published_at = NULL")

    # --- Colas ---------------------------------------------------------------------------------
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
        assert len(mensajes) == cantidad, f"{cola}: {len(mensajes)} de {cantidad} mensajes"
        return mensajes

    def _aviso(self, espera_s: float) -> dict[str, Any] | None:
        url = self.colas["avisos"]
        limite = time.monotonic() + espera_s
        emitida: dict[str, Any] | None = None
        while emitida is None and time.monotonic() < limite:
            respuesta = self.sqs.receive_message(
                QueueUrl=url,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=1,
                VisibilityTimeout=VISIBILIDAD_AVISO_S,
                MessageAttributeNames=["All"],
                AttributeNames=["ApproximateReceiveCount"],
            )
            for mensaje in respuesta.get("Messages", []):
                if tipo_de(mensaje) == "PolizaEmitida":
                    emitida = mensaje
                else:
                    # El aviso del pago sí se envía.
                    self.sqs.delete_message(QueueUrl=url, ReceiptHandle=mensaje["ReceiptHandle"])
        return emitida

    async def recibir_aviso(self, espera_s: float = 30) -> dict[str, Any]:
        """El consumidor de prueba de `avisos`: borra los `RecaudoConfirmado` y devuelve el primer
        `PolizaEmitida` sin borrarlo (el aviso falló): reaparece a los `VISIBILIDAD_AVISO_S`."""
        mensaje = await asyncio.to_thread(self._aviso, espera_s)
        assert mensaje is not None, "avisos: no llegó PolizaEmitida"
        return mensaje

    def borrar_aviso(self, mensaje: dict[str, Any]) -> None:
        self.sqs.delete_message(
            QueueUrl=self.colas["avisos"], ReceiptHandle=mensaje["ReceiptHandle"]
        )

    async def vacia(self, cola: str) -> bool:
        respuesta = await asyncio.to_thread(
            self.sqs.receive_message, QueueUrl=self.colas[cola], WaitTimeSeconds=2
        )
        return not respuesta.get("Messages")

    def en_cola(self, url: str) -> int:
        """Visibles más en vuelo: 0 si todo se borró."""
        atributos = self.sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        return sum(int(valor) for valor in atributos.values())

    def dlqs_vacias(self) -> bool:
        return all(self.en_cola(dlq) == 0 for dlq in self.dlqs.values())

    # --- Lecturas ------------------------------------------------------------------------------
    def polizas_db(self) -> list[dict[str, Any]]:
        return filas(self.url_polizas, "SELECT * FROM polizas")

    def poliza(self, aceptacion_id: str) -> dict[str, Any] | None:
        return next(
            (p for p in self.polizas_db() if str(p["aceptacion_id"]) == aceptacion_id), None
        )

    def emitidas(self) -> list[dict[str, Any]]:
        """Los `PolizaEmitida` del outbox de polizas."""
        return filas(
            self.url_polizas,
            "SELECT event_id, payload, correlation_id, published_at FROM outbox "
            "WHERE event_type = 'PolizaEmitida'",
        )

    def emitidas_publicadas(self) -> bool:
        eventos = self.emitidas()
        return bool(eventos) and all(e["published_at"] is not None for e in eventos)

    def ofertas(self) -> list[dict[str, Any]]:
        return filas(
            self.url_polizas,
            "SELECT event_id, payload, correlation_id FROM outbox "
            "WHERE event_type = 'OfertaAceptada'",
        )

    def inbox_polizas(self) -> list[dict[str, Any]]:
        return filas(self.url_polizas, "SELECT event_id, event_type FROM inbox")

    def recaudo_proyectado(self, clave: str) -> dict[str, Any] | None:
        return next(
            (
                r
                for r in filas(self.url_polizas, "SELECT * FROM recaudos_aceptacion")
                if str(r["clave_recaudo"]) == clave
            ),
            None,
        )

    def movimiento(self, clave: str) -> dict[str, Any] | None:
        return next(
            (
                m
                for m in filas(self.url_dinero, "SELECT * FROM recaudos")
                if str(m["clave_recaudo"]) == clave
            ),
            None,
        )

    def outbox_dinero(self) -> list[dict[str, Any]]:
        return filas(self.url_dinero, "SELECT event_id, event_type, published_at FROM outbox")

    def publicados_dinero(self) -> bool:
        eventos = self.outbox_dinero()
        return bool(eventos) and all(e["published_at"] is not None for e in eventos)

    def versiones(self, clave: str | None = None) -> list[str]:
        """Las versiones del bucket (de esa clave, si se da)."""
        respuesta = self.s3.list_object_versions(Bucket=self.bucket, Prefix=clave or "")
        return [v["VersionId"] for v in respuesta.get("Versions", [])]

    def mediciones(self, operacion: str) -> list[str]:
        return [
            r["result"]
            for r in self.captura.registros()
            if r.get("operation") == operacion and r.get("result")
        ]


# --- Fixtures ----------------------------------------------------------------------------------
def _ejecutar(url: str, sql: str) -> None:
    with psycopg.connect(url, autocommit=True) as conexion:
        conexion.execute(sql)


@pytest.fixture(scope="module")
def base_dinero(base: str) -> str:
    """La base de dinero, otra base en el PostgreSQL de polizas (las dos tienen `outbox`)."""
    _ejecutar(base, "CREATE DATABASE dinero")
    url = f"{base.rsplit('/', 1)[0]}/dinero"
    migrar_dinero(url)
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
def adaptador_pagos(simulador: Simulador) -> Iterator[Servidor]:
    """adaptador-pagos real hacia el simulador, con el timeout al proveedor de prueba (1 s)."""
    servidor = Servidor(
        crear_adaptador_pagos(
            AjustesPagos(
                ALLY_PAGOS_URL=f"{simulador.url}/pagos", ALLY_PAGOS_TIMEOUT_MS=TIMEOUT_PROVEEDOR_MS
            )
        )
    ).arrancar()
    yield servidor
    servidor.apagar()


@pytest.fixture(scope="module")
def adaptador_firma(simulador: Simulador) -> Iterator[Servidor]:
    """adaptador-firma real hacia el simulador, con el timeout al proveedor de prueba (1 s)."""
    servidor = Servidor(
        crear_adaptador_firma(
            AjustesFirma(
                ALLY_FIRMA_URL=f"{simulador.url}/firma", ALLY_FIRMA_TIMEOUT_MS=TIMEOUT_PROVEEDOR_MS
            )
        )
    ).arrancar()
    yield servidor
    servidor.apagar()


@pytest.fixture
def cadena(
    limpia: str,
    base_dinero: str,
    localstack: str,
    credenciales: None,
    bucket: str,
    simulador: Simulador,
    adaptador_pagos: Servidor,
    adaptador_firma: Servidor,
    dobles: Any,
    sinteticos: Any,
) -> Iterator[Cadena]:
    _ejecutar(base_dinero, "TRUNCATE recaudos, outbox, inbox")
    simulador.limpiar_journal()
    raiz = logging.getLogger()
    anteriores, nivel = list(raiz.handlers), raiz.level
    yield Cadena(
        url_polizas=limpia,
        url_dinero=base_dinero,
        localstack=localstack,
        bucket=bucket,
        pagos=adaptador_pagos,
        firma=adaptador_firma,
        simulador=simulador,
        reloj=dobles.reloj,
        sinteticos=sinteticos,
    )
    raiz.handlers, raiz.level = anteriores, nivel


# --- Casos -------------------------------------------------------------------------------------
async def test_c01_de_la_aceptacion_a_la_poliza_activa_verificable_y_su_evento_en_las_colas(
    cadena: Cadena,
) -> None:
    """C01: suscripción y aceptación por la API de polizas → `OfertaAceptada` → dinero cobra
    (APROBADO) → `RecaudoConfirmado` → polizas crea la póliza EMITIENDO → la tarea emisora sube el
    PDF y lo firma por adaptador-firma → ACTIVA → `PolizaEmitida` en avisos, consulta-proyeccion y
    auditoria. La póliza, su documento y su verificación, por la API de polizas."""
    async with cadena.polizas() as api, cadena.dinero():
        aceptacion, correlacion = await cadena.aceptar_s01(api)
        clave = aceptacion["id"]
        await esperar(cadena.emitidas_publicadas, "el relay de polizas no publicó PolizaEmitida")
        avisos = await cadena.recibir("avisos", 2)
        [proyeccion] = await cadena.recibir("consulta-proyeccion", 1)
        auditoria = await cadena.recibir("auditoria", 4)

        consultada = await api.get(
            f"/v1/cotizaciones/{aceptacion['cotizacionId']}/poliza", headers=ASESOR
        )
        assert consultada.status_code == 200, consultada.json()
        poliza = consultada.json()
        enlace = await api.get(f"/v1/polizas/{poliza['polizaId']}/documento", headers=ASESOR)
        assert enlace.status_code == 200, enlace.json()
        async with httpx.AsyncClient(timeout=10) as navegador:
            descarga = await navegador.get(enlace.json()["url"])
        assert descarga.status_code == 200
        verificacion = await api.post(
            VERIFICACIONES,
            json={"numero": poliza["numero"], "huella": sha256(descarga.content)},
            headers=ASESOR,
        )
        await esperar(
            lambda: all(
                cadena.en_cola(cadena.colas[c]) == 0
                for c in ("dinero-aceptaciones", "polizas-recaudos")
            ),
            "dinero o polizas no borraron su mensaje",
        )
        assert await cadena.vacia("avisos") and await cadena.vacia("consulta-proyeccion")

    # Los eventos por tipo, cada uno validado contra su esquema publicado.
    sobres = {tipo_de(m): sobre_de(m) for m in auditoria}
    assert sorted(sobres) == [
        "DecisionSuscripcionRegistrada",
        "OfertaAceptada",
        "PolizaEmitida",
        "RecaudoConfirmado",
    ]
    por_tipo = {tipo_de(m): sobre_de(m) for m in avisos}
    assert sorted(por_tipo) == ["PolizaEmitida", "RecaudoConfirmado"]
    emitida = sobre_de(proyeccion)
    assert por_tipo["PolizaEmitida"] == emitida == sobres["PolizaEmitida"]
    assert por_tipo["RecaudoConfirmado"] == sobres["RecaudoConfirmado"]

    # El recaudo: un cobro aprobado de C01 con la clave de la aceptación.
    assert len(cadena.simulador.cobros(clave)) == 1
    movimiento = cadena.movimiento(clave)
    assert movimiento is not None and movimiento["estado"] == "CONFIRMADO"
    assert sobres["RecaudoConfirmado"]["payload"]["claveRecaudo"] == clave

    # La póliza: una, ACTIVA, con un solo evento y una sola versión del documento.
    [fila] = cadena.polizas_db()
    assert (str(fila["aceptacion_id"]), fila["estado"]) == (clave, "ACTIVA")
    [evento] = cadena.emitidas()
    payload = emitida["payload"]
    assert evento["payload"] == payload
    assert (payload["polizaId"], payload["claveRecaudo"]) == (str(fila["poliza_id"]), clave)
    assert payload["clienteId"] == C01 and payload["numero"] == fila["numero"]
    documento = payload["documento"]
    assert (documento["clave"], documento["versionId"], documento["huella"]) == (
        fila["documento_clave"],
        fila["documento_version_id"],
        fila["documento_huella"],
    )
    assert cadena.versiones(documento["clave"]) == [documento["versionId"]]
    # Las coberturas de la instantánea de la decisión de S01 (17.8b) y el ramo 100.
    assert payload["coberturas"] == [
        {"codigo": c.codigo, "nombre": c.nombre, "obligatoria": c.obligatoria}
        for c in cadena.sinteticos.insumos("S01").coberturas
    ]
    assert payload["ramo"]["codigo"] == "100"
    assert payload["firma"]["firmaId"] == "FIR-SINT-C01"

    # La firma: una, de la huella registrada, por adaptador-firma al proveedor simulado.
    [firma] = cadena.simulador.firmas(payload["polizaId"])
    assert firma["headers"]["Idempotency-Key"] == payload["polizaId"]
    assert json.loads(firma["body"]) == {
        "documentoId": payload["polizaId"],
        "huella": documento["huella"],
        "firmante": C01,
    }

    # La API de polizas: ACTIVA, la URL de la versión registrada, su PDF y su verificación.
    assert (poliza["estado"], poliza["polizaId"], poliza["numero"]) == (
        "ACTIVA",
        payload["polizaId"],
        payload["numero"],
    )
    assert poliza["documento"] == {
        "huella": documento["huella"],
        "versionId": documento["versionId"],
    }
    assert parse_qs(urlsplit(enlace.json()["url"]).query)["versionId"] == [documento["versionId"]]
    assert descarga.content.startswith(b"%PDF-")
    assert sha256(descarga.content) == documento["huella"]
    assert len(descarga.content) == documento["tamanoBytes"]
    assert verificacion.json() == {
        "valido": True,
        "numero": payload["numero"],
        "firmaId": "FIR-SINT-C01",
        "selloTiempo": poliza["firma"]["selloTiempo"],
    }

    # La misma correlación de punta a punta: la de la aceptación.
    assert sobres["OfertaAceptada"]["correlationId"] == correlacion
    assert sobres["RecaudoConfirmado"]["correlationId"] == correlacion
    assert emitida["correlationId"] == correlacion
    assert fila["correlation_id"] == movimiento["correlation_id"] == correlacion
    assert cadena.dlqs_vacias()


async def test_ac3_el_aviso_fallido_recibe_el_mismo_evento_y_el_reintento_no_emite_otra_poliza(
    cadena: Cadena,
) -> None:
    """AC3 (la parte de W17): el consumidor de prueba de `avisos` recibe `PolizaEmitida` y no lo
    borra. Entre tanto, dinero vuelve a publicar `RecaudoConfirmado` (mismo eventId), polizas lo
    descarta como duplicado y la tarea emisora corre otra vez sin nada que emitir. La reentrega
    del aviso trae el mismo evento y la misma referencia de documento."""
    async with cadena.polizas() as api, cadena.dinero():
        aceptacion, _ = await cadena.aceptar_s01(api)
        await esperar(cadena.emitidas_publicadas, "el relay de polizas no publicó PolizaEmitida")
        primero = await cadena.recibir_aviso()

        [recaudo] = cadena.outbox_dinero()
        cadena.republicar_recaudos()
        await esperar(
            lambda: [
                r
                for r in cadena.captura.registros()
                if r.get("event_id") == str(recaudo["event_id"]) and r.get("outcome") == "duplicado"
            ],
            "polizas no recibió la reentrega de RecaudoConfirmado",
        )
        async with cadena.emisora() as emisora:
            assert await emisora.emitir_una_vez() == []

        segundo = await cadena.recibir_aviso()
        cadena.borrar_aviso(segundo)
        await esperar(cadena.publicados_dinero, "el relay de dinero no volvió a marcar")
        await esperar(
            lambda: all(
                cadena.en_cola(cadena.colas[c]) == 0 for c in ("polizas-recaudos", "avisos")
            ),
            "quedaron mensajes en polizas-recaudos o en avisos",
        )

    # La reentrega es el mismo PolizaEmitida, con la misma referencia de documento.
    assert primero["Attributes"]["ApproximateReceiveCount"] == "1"
    assert segundo["Attributes"]["ApproximateReceiveCount"] == "2"
    antes, despues = sobre_de(primero), sobre_de(segundo)
    assert despues == antes
    documento = despues["payload"]["documento"]
    # Una sola póliza, un solo PolizaEmitida en el outbox y una sola versión en S3.
    [fila] = cadena.polizas_db()
    assert (str(fila["aceptacion_id"]), fila["estado"]) == (aceptacion["id"], "ACTIVA")
    [evento] = cadena.emitidas()
    assert str(evento["event_id"]) == despues["eventId"]
    assert (documento["clave"], documento["versionId"], documento["huella"]) == (
        fila["documento_clave"],
        fila["documento_version_id"],
        fila["documento_huella"],
    )
    assert cadena.versiones() == [documento["versionId"]]
    assert len(cadena.simulador.firmas()) == 1
    # El RecaudoConfirmado reentregado entró una sola vez al inbox de polizas.
    assert [(str(e["event_id"]), e["event_type"]) for e in cadena.inbox_polizas()] == [
        (str(recaudo["event_id"]), "RecaudoConfirmado")
    ]
    # Nada fue a la DLQ: dos recepciones de cinco.
    assert cadena.dlqs_vacias()


async def test_c06_firma_lenta_queda_emitiendo_y_el_reintento_la_activa_con_la_misma_version(
    cadena: Cadena,
) -> None:
    """C06: el cobro lento queda PENDIENTE y la conciliación de dinero lo confirma (W16). La firma
    también es lenta: adaptador-firma corta a 1 s (504) y la póliza queda EMITIENDO con el
    documento registrado y `FIRMA_TIEMPO_AGOTADO`. Un mapeo de mayor prioridad firma C06 rápido;
    el reintento la deja ACTIVA con la misma versión del documento."""
    async with cadena.polizas(emisora=False) as api, cadena.dinero() as dinero:
        aceptacion, correlacion = await cadena.aceptar_como(api, C06)
        clave = aceptacion["id"]
        await esperar(
            lambda: (m := cadena.movimiento(clave)) and m["ultimo_incierto"],
            "el cobro no terminó",
        )
        assert cadena.movimiento(clave)["estado"] == "PENDIENTE"  # type: ignore[index]
        await cadena.conciliar(dinero)
        await esperar(lambda: cadena.poliza(clave), "polizas no creó la póliza")

        async with cadena.emisora() as emisora:
            [intento] = await emisora.emitir_una_vez()
        pendiente = cadena.poliza(clave)
        assert pendiente is not None

        mapeo = json.loads(
            (SIMULADOR / "mappings/firma/firma-c06-lento.json").read_text(encoding="utf-8")
        )
        del mapeo["response"]["fixedDelayMilliseconds"]
        rapido = cadena.simulador.agregar(
            mapeo | {"name": "firma por cliente, C06 rápido (R3 de W17)", "priority": 1}
        )
        try:
            cadena.reloj.instante += timedelta(seconds=31)
            async with cadena.emisora() as emisora:
                [reintento] = await emisora.emitir_una_vez()
        finally:
            cadena.simulador.quitar(rapido)
        await esperar(cadena.emitidas_publicadas, "el relay de polizas no publicó PolizaEmitida")
        [proyeccion] = await cadena.recibir("consulta-proyeccion", 1)

    assert (intento.resultado, intento.codigo) == (Resultado.PENDIENTE, "FIRMA_TIEMPO_AGOTADO")
    assert (pendiente["estado"], pendiente["ultimo_fallo"]) == ("EMITIENDO", "FIRMA_TIEMPO_AGOTADO")
    assert pendiente["documento_version_id"] is not None and pendiente["firma_id"] is None
    assert reintento.resultado is Resultado.OK
    activa = cadena.poliza(clave)
    assert activa is not None
    assert (activa["estado"], activa["firma_id"]) == ("ACTIVA", "FIR-SINT-C06")
    assert activa["documento_version_id"] == pendiente["documento_version_id"]
    assert cadena.versiones(activa["documento_clave"]) == [activa["documento_version_id"]]
    # Las dos firmas, de la misma huella registrada y con la misma clave idempotente.
    firmas = cadena.simulador.firmas(str(activa["poliza_id"]))
    assert [json.loads(f["body"])["huella"] for f in firmas] == [activa["documento_huella"]] * 2
    assert {f["headers"]["Idempotency-Key"] for f in firmas} == {str(activa["poliza_id"])}
    emitida = sobre_de(proyeccion)
    assert emitida["payload"]["documento"]["versionId"] == pendiente["documento_version_id"]
    assert emitida["correlationId"] == correlacion
    assert len(cadena.emitidas()) == 1


async def test_c07_firma_caida_sigue_emitiendo_y_pasada_una_hora_se_mide_error_sin_evento(
    cadena: Cadena,
) -> None:
    """C07: la firma responde 503 (FIRMA_NO_DISPONIBLE): EMITIENDO. Con el reloj adelantado más de
    una hora, la medición `emision` sale `error` (alarma) y la póliza sigue EMITIENDO, sin
    `PolizaEmitida`."""
    async with cadena.polizas(emisora=False) as api, cadena.dinero(consumidor=False):
        aceptacion, correlacion = await cadena.aceptar_como(api, C07)
        clave = aceptacion["id"]
        await esperar(
            lambda: cadena.en_cola(cadena.colas["dinero-aceptaciones"]) == 1,
            "el relay de polizas no publicó OfertaAceptada",
        )
        await cadena.confirmar_en_dinero(clave, C07, correlacion)
        await esperar(lambda: cadena.poliza(clave), "polizas no creó la póliza")

        async with cadena.emisora() as emisora:
            [primero] = await emisora.emitir_una_vez()
            cadena.reloj.instante += timedelta(seconds=3601)
            [vencido] = await emisora.emitir_una_vez()
        [aviso] = await cadena.recibir("avisos", 1)
        assert await cadena.vacia("consulta-proyeccion") and await cadena.vacia("avisos")

    assert (primero.resultado, primero.codigo) == (Resultado.PENDIENTE, "FIRMA_NO_DISPONIBLE")
    assert (vencido.resultado, vencido.codigo, vencido.vencida) == (
        Resultado.ERROR,
        "FIRMA_NO_DISPONIBLE",
        True,
    )
    assert cadena.mediciones("emision") == ["pendiente", "error"]
    poliza = cadena.poliza(clave)
    assert poliza is not None
    assert (poliza["estado"], poliza["ultimo_fallo"], poliza["firma_id"]) == (
        "EMITIENDO",
        "FIRMA_NO_DISPONIBLE",
        None,
    )
    assert cadena.emitidas() == []
    # avisos solo recibió el pago (RecaudoConfirmado), con la correlación de la aceptación.
    assert sobre_de(aviso)["type"] == "RecaudoConfirmado"
    assert sobre_de(aviso)["correlationId"] == correlacion
    # El documento se subió una vez (el reintento lo reutiliza) y se pidió la firma dos veces.
    assert cadena.versiones(poliza["documento_clave"]) == [poliza["documento_version_id"]]
    assert len(cadena.simulador.firmas(str(poliza["poliza_id"]))) == 2
    # dinero no cobró a C07: su OfertaAceptada sigue en la cola.
    assert cadena.simulador.cobros(clave) == []


async def test_c02_recaudo_rechazado_no_produce_poliza_y_la_lectura_da_404(
    cadena: Cadena,
) -> None:
    """C02 (AC2): dinero cobra y el proveedor rechaza → `RecaudoRechazado` → polizas lo proyecta y
    no hay póliza, aunque la tarea emisora esté corriendo: `GET …/poliza` responde 404
    POLIZA_NO_ENCONTRADA."""
    async with cadena.polizas() as api, cadena.dinero():
        aceptacion, _ = await cadena.aceptar_como(api, C02)
        clave = aceptacion["id"]
        await esperar(lambda: cadena.recaudo_proyectado(clave), "polizas no proyectó el rechazo")
        # Varios ciclos de la tarea emisora (cada 0,1 s).
        await asyncio.sleep(1)
        respuesta = await api.get(
            f"/v1/cotizaciones/{aceptacion['cotizacionId']}/poliza", headers=ASESOR
        )
        assert await cadena.vacia("avisos")

    assert (respuesta.status_code, respuesta.json()["codigo"]) == (404, "POLIZA_NO_ENCONTRADA")
    assert cadena.recaudo_proyectado(clave)["estado"] == "RECHAZADO"  # type: ignore[index]
    assert cadena.movimiento(clave)["motivo"] == "FONDOS_INSUFICIENTES"  # type: ignore[index]
    assert cadena.polizas_db() == []
    assert cadena.emitidas() == []
    assert cadena.simulador.firmas() == []
    assert cadena.versiones() == []


async def test_dos_tareas_emisoras_sobre_la_misma_poliza_nueva_una_activa_un_evento_una_version(
    cadena: Cadena,
) -> None:
    """La póliza nueva de C01 llega EMITIENDO por la cadena; dos tareas emisoras la toman a la
    vez: una sola la procesa (la reserva), con una sola versión en S3, una firma y un evento."""
    async with cadena.polizas(emisora=False) as api, cadena.dinero():
        aceptacion, _ = await cadena.aceptar_s01(api)
        clave = aceptacion["id"]
        await esperar(lambda: cadena.poliza(clave), "polizas no creó la póliza")
        async with cadena.emisora() as una, cadena.emisora() as otra:
            resultados = await asyncio.gather(una.emitir_una_vez(), otra.emitir_una_vez())
        await esperar(cadena.emitidas_publicadas, "el relay de polizas no publicó PolizaEmitida")
        [proyeccion] = await cadena.recibir("consulta-proyeccion", 1)

    assert [e.resultado for lista in resultados for e in lista] == [Resultado.OK]
    [fila] = cadena.polizas_db()
    assert (fila["estado"], fila["reservada_hasta"]) == ("ACTIVA", None)
    assert cadena.versiones() == [fila["documento_version_id"]]
    assert len(cadena.simulador.firmas()) == 1
    [evento] = cadena.emitidas()
    assert sobre_de(proyeccion)["eventId"] == str(evento["event_id"])
