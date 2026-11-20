"""MT-W22-01, 02 y 03 (N2): pagos-parametricos crea el siniestro de cada activación contra PostgreSQL y
LocalStack reales y contra la app **real** de `siniestros` (HU-W22).

Los consumidores son los de `main.armar_consumidores` (la forma de W24 §9) en modo (b) más `reanudar`
(DS3-15), sobre colas reales con su DLQ (`maxReceiveCount` = 5). `siniestros` corre con uvicorn sobre
su propia base en el mismo PostgreSQL.

- MT-W22-01: el mismo mensaje cinco veces y dos consumidores a la vez → **un** siniestro y **un**
  `SiniestroAprobado`.
- MT-W22-02: un timeout **después** del commit de `siniestros` no duplica: el reintento recibe 200.
- MT-W22-03: `siniestros` caído → reintentos → el mensaje en la DLQ tras 5 recepciones; con
  `siniestros` de vuelta, el reproceso con el mismo `eventId` crea el siniestro una sola vez.

`TESTCONTAINERS_RYUK_DISABLED=true` en macOS.
"""

import asyncio
import copy
import json
import os
import socket
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import psycopg
import pytest
import uvicorn
from pagos_parametricos.adapters.outbound.descartes_pg import migrar
from pagos_parametricos.adapters.outbound.dlq_sqs import DlqSqs
from pagos_parametricos.adapters.outbound.resumen_pg import ResumenPg
from pagos_parametricos.adapters.outbound.siniestros_http import SiniestrosHttp
from pagos_parametricos.application.parametrico.resumen import ConsultarResumen
from pagos_parametricos.config import Settings
from pagos_parametricos.main import armar_consumidores
from psycopg_pool import AsyncConnectionPool
from solventa_common import correlation
from solventa_common.inbox import cliente_sqs
from solventa_common.resiliencia import ProteccionDependencia

docker = pytest.importorskip("docker")
pytestmark = pytest.mark.anyio

IMAGEN_LOCALSTACK = "localstack/localstack:4.14.0"
REGION = "us-east-1"
VISIBILIDAD_S = 2
MAX_RECEPCIONES = 5
EVENTOS = Path(__file__).resolve().parents[4] / "contracts" / "events"
TABLAS = (
    "evaluaciones, activaciones, eventos_evaluados, coberturas_activas, inbox, fallos_consumo, "
    "resoluciones_descarte, outbox"
)
TABLAS_SINIESTROS = (
    "borrador_evidencias, borradores, siniestros, evidencias, historial_siniestro, "
    "decisiones_siniestro, outbox, inbox, idempotencia, secuencia_codigo"
)


@pytest.fixture
def anyio_backend() -> tuple[str, dict[str, Any]]:
    return "asyncio", {"loop_factory": asyncio.SelectorEventLoop}


def _docker_disponible() -> None:
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
    try:
        docker.from_env().ping()
    except docker.errors.DockerException:
        pytest.skip("Docker no está disponible para testcontainers")


@pytest.fixture(scope="module")
def postgres() -> Iterator[str]:
    _docker_disponible()
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("postgres:16", driver=None) as contenedor:
        url: str = contenedor.get_connection_url()
        yield url


@pytest.fixture(scope="module")
def base(postgres: str) -> str:
    migrar(postgres)
    return postgres


@pytest.fixture(scope="module")
def base_siniestros(postgres: str) -> str:
    """La base de `siniestros`, aparte, en el mismo PostgreSQL (un servicio, una base)."""
    with psycopg.connect(postgres, autocommit=True) as conexion:
        conexion.execute("CREATE DATABASE siniestros_w22")
    return postgres.rsplit("/", 1)[0] + "/siniestros_w22"


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
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


def sql(url: str, consulta: str, *parametros: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conexion:
        cursor = conexion.execute(consulta, parametros)
        return cursor.fetchall() if cursor.description else []


async def esperar(condicion: Callable[[], Any], plazo_s: float = 45.0) -> Any:
    limite = asyncio.get_running_loop().time() + plazo_s
    while True:
        valor = await asyncio.to_thread(condicion)
        if valor:
            return valor
        if asyncio.get_running_loop().time() > limite:
            raise AssertionError("la condición no se cumplió a tiempo")
        await asyncio.sleep(0.2)


@contextmanager
def servir(app: Any, puerto: int | None = None) -> Iterator[str]:
    zocalo = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    zocalo.bind(("127.0.0.1", puerto or 0))
    url = f"http://127.0.0.1:{zocalo.getsockname()[1]}"
    servidor = uvicorn.Server(uvicorn.Config(app, log_level="warning"))

    def correr() -> None:
        asyncio.run(servidor.serve(sockets=[zocalo]), loop_factory=asyncio.SelectorEventLoop)

    hilo = threading.Thread(target=correr, daemon=True)
    hilo.start()
    limite = time.monotonic() + 30
    while not servidor.started:
        assert hilo.is_alive(), "siniestros no arrancó"
        assert time.monotonic() < limite, "siniestros no arrancó a tiempo"
        time.sleep(0.05)
    try:
        yield url
    finally:
        servidor.should_exit = True
        hilo.join(timeout=15)


@pytest.fixture
def siniestros_real(base_siniestros: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """La app real de `siniestros` (uvicorn) sobre su base, limpia en cada prueba."""
    from siniestros.config import Settings as SettingsSiniestros
    from siniestros.main import crear_app

    monkeypatch.setenv("SOLVENTA_DATABASE_URL", base_siniestros)
    with servir(crear_app(SettingsSiniestros())) as url:
        sql(base_siniestros, f"TRUNCATE {TABLAS_SINIESTROS}")
        yield url


class TransporteLentoTrasElCommit(httpx.AsyncBaseTransport):
    """Reenvía la solicitud al `siniestros` real y **después** de su respuesta (con el commit hecho)
    se demora: el cliente de `pagos-parametricos` agota su timeout sin ver la respuesta."""

    def __init__(self, demora_s: float) -> None:
        self.demora_s = demora_s
        self.demoras = 0
        self._real = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        respuesta = await self._real.handle_async_request(request)
        await respuesta.aread()
        if self.demoras > 0:
            self.demoras -= 1
            await asyncio.sleep(self.demora_s)
        return respuesta


@dataclass
class Entorno:
    sns: Any
    sqs: Any
    topico: str
    url_eventos: str
    url_polizas: str
    pool: AsyncConnectionPool[Any]
    settings: Settings
    base: str
    base_siniestros: str
    detener: Callable[[], Any]

    @property
    def dlq_eventos(self) -> str:
        return f"{self.url_eventos}-dlq"

    def publicar(self, sobre: dict[str, Any]) -> None:
        self.sns.publish(
            TopicArn=self.topico,
            Message=json.dumps(sobre),
            MessageAttributes={"eventType": {"DataType": "String", "StringValue": sobre["type"]}},
        )

    def enviar(self, cuerpo: str, tipo: str, url: str | None = None) -> None:
        self.sqs.send_message(
            QueueUrl=url or self.url_eventos,
            MessageBody=cuerpo,
            MessageAttributes={"eventType": {"DataType": "String", "StringValue": tipo}},
        )

    def en_dlq(self) -> int:
        atributos = self.sqs.get_queue_attributes(
            QueueUrl=self.dlq_eventos, AttributeNames=["ApproximateNumberOfMessages"]
        )["Attributes"]
        return int(atributos["ApproximateNumberOfMessages"])

    def en_cola(self) -> int:
        atributos = self.sqs.get_queue_attributes(
            QueueUrl=self.url_eventos,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        return int(atributos["ApproximateNumberOfMessages"]) + int(
            atributos["ApproximateNumberOfMessagesNotVisible"]
        )

    def tabla(self, nombre: str) -> int:
        return int(sql(self.base, f"SELECT count(*) FROM {nombre}")[0][0])

    def en_siniestros(self, nombre: str, donde: str = "true") -> int:
        return int(sql(self.base_siniestros, f"SELECT count(*) FROM {nombre} WHERE {donde}")[0][0])


def _crear_cola(sqs: Any, nombre: str) -> str:
    dlq = sqs.create_queue(QueueName=f"{nombre}-dlq")["QueueUrl"]
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]
    url: str = sqs.create_queue(
        QueueName=nombre,
        Attributes={
            "VisibilityTimeout": str(VISIBILIDAD_S),
            "RedrivePolicy": json.dumps(
                {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": MAX_RECEPCIONES}
            ),
        },
    )["QueueUrl"]
    return url


@pytest.fixture
def montar(
    base: str,
    base_siniestros: str,
    localstack: str,
    credenciales: None,
    siniestros_real: str,
) -> Callable[..., Any]:
    """Una fábrica de entornos: `montar(url_siniestros=…, transporte=…, consumidores=…)`."""
    sql(base, f"TRUNCATE {TABLAS}")

    @asynccontextmanager
    async def fabricar(
        *,
        url_siniestros: str | None = None,
        transporte: httpx.AsyncBaseTransport | None = None,
        conjuntos: int = 1,
    ) -> AsyncIterator[Entorno]:
        import boto3

        sns = boto3.client("sns", region_name=REGION, endpoint_url=localstack)
        sqs = cliente_sqs(REGION, endpoint_url=localstack, espera_larga_s=1)
        sufijo = uuid4().hex[:8]
        topico = sns.create_topic(Name=f"eventos-{sufijo}")["TopicArn"]
        url_eventos = _crear_cola(sqs, f"parametricos-eventos-{sufijo}")
        url_polizas = _crear_cola(sqs, f"parametricos-polizas-{sufijo}")
        for url, filtro in (
            (url_eventos, {"eventType": ["EventoParametricoObservado"]}),
            (url_polizas, {"eventType": [{"prefix": "Poliza"}]}),
        ):
            arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"][
                "QueueArn"
            ]
            sns.subscribe(
                TopicArn=topico,
                Protocol="sqs",
                Endpoint=arn,
                Attributes={"RawMessageDelivery": "true", "FilterPolicy": json.dumps(filtro)},
            )
        settings = Settings(
            SQS_PARAMETRICOS_EVENTOS_URL=url_eventos,
            SQS_PARAMETRICOS_POLIZAS_URL=url_polizas,
            AWS_ENDPOINT_URL=localstack,
            SQS_CONCURRENCIA=10,
            SINIESTROS_URL=url_siniestros or siniestros_real,
        )
        async with (
            AsyncConnectionPool(
                base, min_size=1, max_size=settings.pool_max * 2, open=False
            ) as pool,
            correlation.cliente_async(transport=transporte) as http,
        ):
            acl = SiniestrosHttp(
                http,
                settings.siniestros_url,
                settings.siniestros_timeout_ms,
                ProteccionDependencia(
                    settings.cb_fallos_consecutivos, 1, settings.siniestros_bulkhead_max
                ),
            )
            from pagos_parametricos.adapters.outbound.reloj import RelojSistema

            parada = asyncio.Event()
            tareas = []
            for _ in range(conjuntos):
                consumidores = armar_consumidores(
                    pool,
                    sqs,
                    RelojSistema(),
                    settings,
                    acl,
                    visibilidad_s=VISIBILIDAD_S,
                    espera_larga_s=1,
                )
                tareas += [asyncio.create_task(c.ejecutar(parada)) for c in consumidores.values()]
            try:
                yield Entorno(
                    sns, sqs, topico, url_eventos, url_polizas, pool, settings, base,
                    base_siniestros, parada.set,
                )  # fmt: skip
            finally:
                parada.set()
                await asyncio.wait_for(asyncio.gather(*tareas), 25)

    return fabricar


async def _cargar_coberturas(entorno: Entorno, datos: dict[str, Any]) -> None:
    for entrada in datos["coberturas"]:
        entorno.publicar(copy.deepcopy(entrada["sobre"]))
    await esperar(lambda: entorno.tabla("coberturas_activas") == len(datos["coberturas"]))


def _hay_siniestro_creado(entorno: Entorno) -> Any:
    return entorno.tabla("activaciones") and sql(
        entorno.base, "SELECT count(*) FROM activaciones WHERE estado = 'SINIESTRO_CREADO'"
    )[0][0] == entorno.tabla("activaciones")


# --- MT-W22-01 ------------------------------------------------------------------------------------
async def test_un_evento_que_activa_crea_un_siniestro_y_el_mensaje_se_borra(
    montar: Callable[..., Any],
    datos: dict[str, Any],
    casos: dict[str, dict[str, Any]],
    observado: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    async with montar() as entorno:
        await _cargar_coberturas(entorno, datos)
        sobre = observado(casos["valido"])
        entorno.publicar(sobre)
        await esperar(lambda: _hay_siniestro_creado(entorno))
        # El mensaje se borra solo después de que `siniestros` respondió: nada en cola ni en la DLQ.
        await esperar(lambda: entorno.en_cola() == 0)
        assert entorno.en_dlq() == 0

        [activacion] = sql(
            entorno.base,
            "SELECT estado, siniestro_id, siniestro_codigo, disparador, valor_pago, moneda "
            "FROM activaciones",
        )
        estado, siniestro_id, codigo, disparador, valor, moneda = activacion
        assert (estado, moneda, valor) == ("SINIESTRO_CREADO", "COP", 1400000)
        assert disparador == {
            "tipoEvento": "LLUVIA_ACUMULADA",
            "valorObservado": "135",
            "umbral": "120.000",
            "unidad": "mm",
        }
        # El siniestro es el que describe AC1 y AC3: APROBADO, sin analista, con el evento y la regla.
        [fila] = sql(
            entorno.base_siniestros,
            "SELECT id, codigo, origen, estado, analista_id, valor_indemnizar, deducible, "
            "evento_id, regla, evento, cliente_id, poliza_id FROM siniestros",
        )
        assert (fila[0], fila[1]) == (siniestro_id, codigo)
        assert fila[2:7] == ("PARAMETRICO", "APROBADO", None, 1400000, 0)
        assert str(fila[7]) == sobre["eventId"] and fila[8] == {
            "codigo": "VALOR_IGUAL_O_SUPERIOR_AL_UMBRAL",
            "version": "1",
        }
        assert fila[9]["fuente"] == "IDEAM-SIM" and fila[9]["valorObservado"] == "135"
        # El titular y la póliza salen de la copia de la cobertura (R-177).
        cobertura = next(c for c in datos["coberturas"] if c["clave"] == "COB-BOG-1")["sobre"]
        assert str(fila[10]) == cobertura["payload"]["clienteId"]
        assert str(fila[11]) == cobertura["payload"]["polizaId"]
        # Un solo SiniestroAprobado en el Outbox de siniestros, sin quien decide.
        [(tipo, payload)] = sql(entorno.base_siniestros, "SELECT event_type, payload FROM outbox")
        assert tipo == "SiniestroAprobado" and payload["decididoPor"] is None
        assert (
            payload["origen"] == "PARAMETRICO" and payload["referenciaPago"] == f"IND-{codigo}-01"
        )


async def test_el_mismo_mensaje_cinco_veces_y_dos_consumidores_a_la_vez_dejan_un_solo_siniestro(
    montar: Callable[..., Any],
    datos: dict[str, Any],
    casos: dict[str, dict[str, Any]],
    observado: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    async with montar(conjuntos=2) as entorno:
        await _cargar_coberturas(entorno, datos)
        cuerpo = json.dumps(observado(casos["valido"]))
        for _ in range(5):  # SQS entrega al menos una vez
            entorno.enviar(cuerpo, "EventoParametricoObservado")
        await esperar(lambda: _hay_siniestro_creado(entorno))
        await esperar(lambda: entorno.en_cola() == 0)

        assert entorno.tabla("eventos_evaluados") == 1 and entorno.tabla("activaciones") == 1
        assert entorno.en_siniestros("siniestros") == 1
        assert entorno.en_siniestros("outbox", "event_type = 'SiniestroAprobado'") == 1
        assert (
            entorno.en_siniestros("historial_siniestro", "accion = 'CREADO_AUTOMATICAMENTE'") == 1
        )
        assert entorno.en_dlq() == 0


async def test_dos_eventos_a_la_vez_para_la_misma_cobertura_crean_un_siniestro_por_evento_y_poliza(
    montar: Callable[..., Any],
    datos: dict[str, Any],
    casos: dict[str, dict[str, Any]],
    observado: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    async with montar(conjuntos=2) as entorno:
        await _cargar_coberturas(entorno, datos)
        for clave in ("valido", "justoEnElUmbral"):
            entorno.publicar(observado(casos[clave]))
        await esperar(lambda: entorno.tabla("activaciones") == 2 and _hay_siniestro_creado(entorno))

        assert entorno.en_siniestros("siniestros") == 2
        codigos = sql(entorno.base_siniestros, "SELECT DISTINCT codigo FROM siniestros")
        assert len(codigos) == 2  # un código por siniestro, sin repetirse


# --- MT-W22-02 ------------------------------------------------------------------------------------
async def test_un_timeout_despues_del_commit_de_siniestros_no_duplica(
    montar: Callable[..., Any],
    siniestros_real: str,
    datos: dict[str, Any],
    casos: dict[str, dict[str, Any]],
    observado: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    transporte = TransporteLentoTrasElCommit(demora_s=2.0)
    transporte.demoras = 1  # solo la primera respuesta se demora; el reintento llega a tiempo
    async with montar(transporte=transporte) as entorno:
        await _cargar_coberturas(entorno, datos)
        entorno.publicar(observado(casos["valido"]))
        # La primera llamada hace commit y agota el timeout (600 ms): el mensaje vuelve tras la
        # visibilidad y `reanudar` recibe 200 con el siniestro que ya existía.
        await esperar(lambda: entorno.en_siniestros("siniestros") == 1)
        await esperar(lambda: _hay_siniestro_creado(entorno))
        await esperar(lambda: entorno.en_cola() == 0)

        assert transporte.demoras == 0  # la demora se aplicó
        assert entorno.en_siniestros("siniestros") == 1
        assert entorno.en_siniestros("outbox", "event_type = 'SiniestroAprobado'") == 1
        assert entorno.en_dlq() == 0
        # La entrega repetida se contó (DS3-15), sin cambiar la decisión.
        [(duplicados,)] = sql(entorno.base, "SELECT duplicados FROM eventos_evaluados")
        assert duplicados >= 1


# --- MT-W22-03 ------------------------------------------------------------------------------------
async def test_con_siniestros_caido_el_mensaje_llega_a_la_dlq_y_el_reproceso_crea_un_solo_siniestro(
    montar: Callable[..., Any],
    datos: dict[str, Any],
    casos: dict[str, dict[str, Any]],
    observado: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    with socket.socket() as libre:
        libre.bind(("127.0.0.1", 0))
        puerto = libre.getsockname()[1]  # nadie escucha: `siniestros` caído
    async with montar(url_siniestros=f"http://127.0.0.1:{puerto}") as entorno:
        await _cargar_coberturas(entorno, datos)
        sobre = observado(casos["valido"])
        entorno.publicar(sobre)
        # AC4: el mensaje se reintenta y, tras 5 recepciones, queda en la DLQ de `parametricos-eventos`.
        await esperar(lambda: entorno.en_dlq() == 1, plazo_s=90)
        assert entorno.en_siniestros("siniestros") == 0
        [(estado, siniestro_id)] = sql(
            entorno.base, "SELECT estado, siniestro_id FROM activaciones"
        )
        assert (estado, siniestro_id) == ("PENDIENTE", None)
        # La transacción de W21 se hizo una sola vez y las entregas se contaron.
        assert entorno.tabla("eventos_evaluados") == 1
        [(duplicados,)] = sql(entorno.base, "SELECT duplicados FROM eventos_evaluados")
        assert duplicados >= MAX_RECEPCIONES - 2
        # La causa que H10 muestra: el nombre de la excepción de la continuación.
        causas = sql(
            entorno.base,
            "SELECT DISTINCT detalle FROM fallos_consumo WHERE outcome = 'fallo_continuacion'",
        )
        assert ("ActivacionFallida",) in causas

    # `siniestros` de vuelta (otra app con el servicio real): el reproceso con el mismo `eventId`.
    cuerpo = json.dumps(sobre)
    async with montar() as recuperado:
        recuperado.enviar(cuerpo, "EventoParametricoObservado")
        recuperado.enviar(cuerpo, "EventoParametricoObservado")
        await _cargar_coberturas_si_faltan(recuperado, datos)
        await esperar(lambda: _hay_siniestro_creado(recuperado))
        assert recuperado.en_siniestros("siniestros") == 1
        assert recuperado.en_siniestros("outbox", "event_type = 'SiniestroAprobado'") == 1


async def _cargar_coberturas_si_faltan(entorno: Entorno, datos: dict[str, Any]) -> None:
    """`montar` vacía las tablas de `pagos-parametricos`: el entorno recuperado las vuelve a cargar."""
    if entorno.tabla("coberturas_activas") < len(datos["coberturas"]):
        await _cargar_coberturas(entorno, datos)


async def test_una_cobertura_anterior_a_la_migracion_sin_cliente_no_crea_siniestro_y_va_a_la_dlq(
    montar: Callable[..., Any],
    datos: dict[str, Any],
    casos: dict[str, dict[str, Any]],
    observado: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    async with montar() as entorno:
        await _cargar_coberturas(entorno, datos)
        # Una cobertura aplicada antes de la migración 003: sin titular ni ramo (R-177).
        sql(entorno.base, "UPDATE coberturas_activas SET cliente_id = NULL, ramo = NULL")
        entorno.publicar(observado(casos["valido"]))

        await esperar(lambda: entorno.en_dlq() == 1, plazo_s=90)

        assert entorno.en_siniestros("siniestros") == 0
        [(estado,)] = sql(entorno.base, "SELECT estado FROM activaciones")
        assert estado == "PENDIENTE"


# --- El seguimiento de H09 ---------------------------------------------------------------------------
async def test_el_resumen_de_h09_cuenta_el_evento_la_activacion_y_el_ultimo_pago(
    montar: Callable[..., Any],
    datos: dict[str, Any],
    casos: dict[str, dict[str, Any]],
    observado: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    from pagos_parametricos.adapters.outbound.reloj import RelojSistema

    async with montar() as entorno:
        await _cargar_coberturas(entorno, datos)
        entorno.publicar(observado(casos["valido"]))
        await esperar(lambda: _hay_siniestro_creado(entorno))
        dlq = DlqSqs(entorno.sqs, entorno.url_eventos)
        servicio = ConsultarResumen(ResumenPg(entorno.pool), dlq, RelojSistema())

        resumen = await servicio.resumen(None)

        assert resumen.evento_origen == "EVT-2026-0913"
        assert (resumen.procesados, resumen.activados, resumen.en_dlq) == (1, 1, 0)
        assert resumen.recibidos == resumen.procesados + resumen.duplicados
        assert sum(m.procesados for m in resumen.ritmo_por_minuto) == 1
        (pago,) = resumen.ultimos_pagos
        assert pago.estado_activacion == "SINIESTRO_CREADO" and pago.valor == "1400000.00"
        assert pago.disparador.valor_observado == "135" and pago.poliza_numero is not None
        origenes = await servicio.origenes(10)
        assert [(o.evento_origen, o.eventos) for o in origenes] == [("EVT-2026-0913", 1)]


# --- La migración 003 --------------------------------------------------------------------------------
def test_la_migracion_003_va_adelante_y_atras_y_exige_el_siniestro_con_el_estado(base: str) -> None:
    ahora = datetime(2026, 11, 10, 14, 0, tzinfo=UTC)
    evento = uuid4()
    sql(base, f"TRUNCATE {TABLAS}")
    sql(
        base,
        "INSERT INTO eventos_evaluados (evento_id, fuente, id_externo, evento_origen, decision, "
        "razon, evaluadas, activaciones, version_regla, evaluado_en) "
        "VALUES (%s, 'IDEAM-SIM', 'X-1', 'EVT-1', 'ACTIVA', NULL, 1, 1, '1', %s)",
        evento,
        ahora,
    )
    insertar = (
        "INSERT INTO activaciones (evento_id, poliza_id, estado, siniestro_id, valor_pago, moneda, "
        "version_regla, creada_en) VALUES (%s, %s, %s, %s, 1, 'COP', '1', %s)"
    )
    # PENDIENTE sin siniestro y SINIESTRO_CREADO con él son válidos; las otras combinaciones, no.
    sql(base, insertar, evento, uuid4(), "PENDIENTE", None, ahora)
    sql(base, insertar, evento, uuid4(), "SINIESTRO_CREADO", uuid4(), ahora)
    with pytest.raises(psycopg.errors.CheckViolation):
        sql(base, insertar, evento, uuid4(), "SINIESTRO_CREADO", None, ahora)
    with pytest.raises(psycopg.errors.CheckViolation):
        sql(base, insertar, evento, uuid4(), "PENDIENTE", uuid4(), ahora)

    migraciones = (
        Path(__file__).resolve().parents[2] / "src/pagos_parametricos/adapters/outbound/migraciones"
    )
    reversa = (migraciones / "down/003_activacion_siniestro.sql").read_text(encoding="utf-8")
    sql(base, reversa)
    columnas = {
        f[0]
        for f in sql(
            base,
            "SELECT column_name FROM information_schema.columns WHERE table_name IN ('activaciones', 'coberturas_activas')",
        )
    }
    assert not {"siniestro_codigo", "disparador", "cliente_id", "ramo"} & columnas
    sql(base, reversa)  # repetirla no falla
    migrar(base)
    migrar(base)  # idempotente
    columnas = {
        f[0]
        for f in sql(
            base,
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'activaciones'",
        )
    }
    assert {"siniestro_codigo", "disparador"} <= columnas
