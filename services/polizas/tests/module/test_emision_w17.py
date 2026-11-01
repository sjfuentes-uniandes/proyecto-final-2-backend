"""MT-W17-08 (17.8): la tarea emisora contra PostgreSQL, LocalStack (S3, SNS y SQS) y WireMock con
las respuestas del contrato de adaptador-firma.

- De punta a punta: un `RecaudoConfirmado` en `polizas-recaudos` → póliza ACTIVA y `PolizaEmitida`
  en el outbox; con el relay, lo reciben una cola con el filtro exacto de `avisos` y otra con el
  prefijo `Poliza` de `consulta-proyeccion`, y el cuerpo cumple el esquema publicado. La URL
  prefirmada de la versión registrada descarga un PDF cuyo sha256 es la huella del evento.
- Firma lenta → EMITIENDO → reintento → ACTIVA con la misma versión del documento (una sola).
- S3 caído → EMITIENDO → se recupera; una caída tras registrar el documento no sube otro.
- Dos tareas emisoras concurrentes: una sola ACTIVA y un solo `PolizaEmitida` por póliza.
- `RecaudoRechazado` no produce póliza; una pendiente de más de una hora se mide como `error`.
- La migración 005 hacia adelante, hacia atrás y otra vez hacia adelante.
"""

import asyncio
import hashlib
import json
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from importlib import resources
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import boto3
import httpx
import psycopg
import pytest
from jsonschema import Draft202012Validator
from polizas.adapters.outbound.decisiones_pg import migrar
from polizas.adapters.outbound.documentos_s3 import AlmacenDocumentosS3, cliente_s3
from polizas.adapters.outbound.firma_http import AdaptadorFirmaHttp
from polizas.adapters.outbound.polizas_pg import PolizasPg
from polizas.adapters.outbound.recaudos_pg import RecaudosPg
from polizas.application.poliza.emitir import EmitirPolizas, Resultado
from polizas.application.recaudo.proyectar import ProyectarRecaudo
from polizas.config import Settings
from polizas.domain.recaudo.modelos import ResultadoRecaudo, SituacionRecaudo
from polizas.main import armar_emisora
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from solventa_common import correlation
from solventa_common.inbox import InboxPg

docker = pytest.importorskip("docker")
pytestmark = pytest.mark.anyio
REGION = "us-east-1"
MIGRACIONES = resources.files("polizas.adapters.outbound").joinpath("migraciones")
EVENTOS = Path(__file__).parents[4] / "contracts/events"
ASESOR = {"X-Actor-Id": "sub-asesor-1", "X-Actor-Rol": "ASESOR"}
TODAS = {
    "leidoCondiciones": True,
    "informacionVeraz": True,
    "debitoAutomatico": True,
    "avisosElectronicos": True,
}
RESUELTO_EN = "2026-10-22T15:00:01.250000Z"
CORRELACION = "corr-emision-modulo"
C01 = "00000000-0000-4000-8000-000000000001"


# --- Ayudas ------------------------------------------------------------------------------------
def _filas(url: str, consulta: str, parametros: Any = None) -> list[dict[str, Any]]:
    with psycopg.connect(url, autocommit=True) as conexion:
        return conexion.cursor(row_factory=dict_row).execute(consulta, parametros).fetchall()


def _poliza(url: str, aceptacion_id: UUID) -> dict[str, Any]:
    [fila] = _filas(url, "SELECT * FROM polizas WHERE aceptacion_id = %s", (aceptacion_id,))
    return fila


def _emitidas(url: str) -> list[dict[str, Any]]:
    return _filas(
        url,
        "SELECT aggregate_id, correlation_id, occurred_at, payload FROM outbox "
        "WHERE event_type = 'PolizaEmitida' ORDER BY aggregate_id",
    )


async def _esperar(condicion: Callable[[], Awaitable[bool] | bool], limite_s: float = 30) -> None:
    fin = time.monotonic() + limite_s
    while True:
        resultado = condicion()
        if await resultado if isinstance(resultado, Awaitable) else resultado:
            return
        assert time.monotonic() < fin, "la condición no se cumplió a tiempo"
        await asyncio.sleep(0.1)


def _sobre(tipo: str, clave: UUID) -> dict[str, Any]:
    recaudo_id = str(uuid4())
    datos: dict[str, Any] = {
        "recaudoId": recaudo_id,
        "claveRecaudo": str(clave),
        "cotizacionId": str(uuid4()),
    }
    if tipo == "RecaudoConfirmado":
        datos |= {
            "monto": {"valor": "64638.10", "moneda": "COP"},
            "transaccionId": "TX-SINTETICA-0001",
            "confirmadoEn": RESUELTO_EN,
        }
    else:
        datos |= {"motivo": "FONDOS_INSUFICIENTES", "rechazadoEn": RESUELTO_EN}
    return {
        "eventId": str(uuid4()),
        "aggregateId": recaudo_id,
        "type": tipo,
        "version": 1,
        "occurredAt": RESUELTO_EN,
        "correlationId": CORRELACION,
        "payload": datos,
    }


async def _aceptar(api: Any, sinteticos: Any) -> UUID:
    """Suscripción y aceptación de S01 por la API de polizas; devuelve la clave (= su id)."""
    decision = await api.post(
        "/v1/suscripciones",
        json={"cotizacionId": sinteticos.cotizacion_id("S01")},
        headers={**ASESOR, "Idempotency-Key": str(uuid4())},
    )
    assert decision.status_code == 201, decision.json()
    cuerpo = decision.json()
    aceptacion = await api.post(
        f"/v1/cotizaciones/{cuerpo['cotizacionId']}/aceptacion",
        json={
            "decisionId": cuerpo["id"],
            "versionDeclaraciones": "2026.10.1",
            "declaraciones": TODAS,
        },
        headers={**ASESOR, "Idempotency-Key": str(uuid4())},
    )
    assert aceptacion.status_code == 201, aceptacion.json()
    return UUID(aceptacion.json()["id"])


def _copiar_aceptacion(url: str, aceptacion_id: UUID) -> UUID:
    """Otra aceptación conocida (solo S01 sale APROBADA): copia la decisión y la aceptación con
    identificadores nuevos (como test_poliza_w17)."""
    nueva_decision, nueva = uuid4(), uuid4()
    with psycopg.connect(url, autocommit=True) as conexion:
        cursor = conexion.cursor(row_factory=dict_row)
        aceptacion = cursor.execute(
            "SELECT * FROM aceptaciones WHERE id = %s", (aceptacion_id,)
        ).fetchone()
        assert aceptacion is not None
        decision = cursor.execute(
            "SELECT * FROM decisiones_suscripcion WHERE id = %s", (aceptacion["decision_id"],)
        ).fetchone()
        assert decision is not None
        decision |= {
            "id": nueva_decision,
            "codigo": f"DEC-2026-{random.randint(900_000, 999_999)}",
            "cotizacion_id": uuid4(),
            "idempotency_key": uuid4(),
        }
        aceptacion |= {
            "id": nueva,
            "cotizacion_id": decision["cotizacion_id"],
            "decision_id": nueva_decision,
            "idempotency_key": uuid4(),
        }
        for tabla, fila in (("decisiones_suscripcion", decision), ("aceptaciones", aceptacion)):
            valores = [Jsonb(v) if isinstance(v, dict | list) else v for v in fila.values()]
            conexion.execute(
                f"INSERT INTO {tabla} ({', '.join(fila)}) VALUES ({', '.join(['%s'] * len(fila))})",
                valores,
            )
    return nueva


def _resultado(clave: UUID) -> ResultadoRecaudo:
    from datetime import UTC, datetime
    from decimal import Decimal

    return ResultadoRecaudo(
        clave_recaudo=clave,
        recaudo_id=uuid4(),
        estado=SituacionRecaudo.CONFIRMADO,
        monto=Decimal("64638.10"),
        moneda="COP",
        transaccion_id="TX-SINTETICA-0001",
        motivo=None,
        ocurrido_en=datetime(2026, 10, 22, 15, 0, 1, tzinfo=UTC),
    )


async def _confirmar(url: str, dobles: Any, *claves: UUID) -> None:
    """El primer RecaudoConfirmado de cada clave, como el consumidor (crea la póliza EMITIENDO)."""
    from psycopg_pool import AsyncConnectionPool

    async with AsyncConnectionPool(url, min_size=1, max_size=2, open=False) as pool:
        inbox = InboxPg(pool)
        caso = ProyectarRecaudo(RecaudosPg(pool), PolizasPg(), dobles.reloj)
        for clave in claves:
            async with inbox.transaccion() as tx:
                await caso.ejecutar(_resultado(clave), tx, CORRELACION)


def _versiones(localstack: str, bucket: str, clave: str) -> list[str]:
    s3 = boto3.client("s3", region_name=REGION, endpoint_url=localstack)
    respuesta = s3.list_object_versions(Bucket=bucket, Prefix=clave)
    return [v["VersionId"] for v in respuesta.get("Versions", [])]


def _sha256(contenido: bytes) -> str:
    return "sha256:" + hashlib.sha256(contenido).hexdigest()


def _validar(cuerpo: dict[str, Any]) -> None:
    esquema = json.loads((EVENTOS / "PolizaEmitida.v1.schema.json").read_text(encoding="utf-8"))
    validador = Draft202012Validator(esquema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    assert [e.message for e in validador.iter_errors(cuerpo)] == []


@asynccontextmanager
async def _emisora(
    url: str,
    dobles: Any,
    localstack: str,
    bucket: str,
    firma_url: str,
    *,
    endpoint_s3: str | None = None,
) -> AsyncIterator[EmitirPolizas[Any]]:
    """Una tarea emisora de producción (`armar_emisora`) sobre su propio pool."""
    from psycopg_pool import AsyncConnectionPool

    settings = Settings(
        POLIZAS_BUCKET=bucket, AWS_REGION=REGION, AWS_ENDPOINT_URL=localstack, emision_lote=10
    )
    almacen = AlmacenDocumentosS3(
        cliente_s3(REGION, endpoint_url=endpoint_s3 or localstack),
        bucket,
        settings.polizas_bucket_sse,
    )
    async with (
        AsyncConnectionPool(url, min_size=1, max_size=4, open=False) as pool,
        correlation.cliente_async() as cliente,
    ):
        firma = AdaptadorFirmaHttp(cliente, firma_url, settings.adaptador_firma_timeout_ms)
        yield armar_emisora(pool, almacen, firma, dobles.reloj, settings)


# --- De punta a punta --------------------------------------------------------------------------
async def test_de_un_recaudo_confirmado_en_la_cola_a_poliza_activa_y_su_evento_en_las_colas(
    abrir_api_emisora: Any,
    cola_recaudos: Any,
    colas_poliza: Any,
    firma: Any,
    sinteticos: Any,
    limpia: str,
    localstack: str,
    bucket: str,
) -> None:
    async with abrir_api_emisora(relay=True) as api:
        clave = await _aceptar(api, sinteticos)
        await asyncio.to_thread(cola_recaudos.publicar, _sobre("RecaudoConfirmado", clave))
        await _esperar(lambda: _filas(limpia, "SELECT 1 FROM polizas WHERE estado = 'ACTIVA'"))
        mensajeria = colas_poliza.mensajeria
        avisos = await asyncio.to_thread(mensajeria.recibir, colas_poliza.avisos, 1)
        consulta = await asyncio.to_thread(mensajeria.recibir, colas_poliza.consulta, 1)

    poliza = _poliza(limpia, clave)
    assert poliza["estado"] == "ACTIVA" and poliza["ultimo_fallo"] is None
    # emitidaEn = fechaDocumento = creada_en (17.7).
    assert poliza["emitida_en"] == poliza["documento_fecha"] == poliza["creada_en"]
    assert poliza["correlation_id"] == CORRELACION
    [evento] = _emitidas(limpia)
    assert (evento["aggregate_id"], evento["correlation_id"]) == (poliza["poliza_id"], CORRELACION)
    assert evento["occurred_at"] == poliza["emitida_en"]

    # avisos (filtro exacto) y consulta-proyeccion (prefijo Poliza) reciben el mismo evento.
    [aviso], [proyeccion] = avisos, consulta
    for mensaje in (aviso, proyeccion):
        assert mensaje["MessageAttributes"]["eventType"]["StringValue"] == "PolizaEmitida"
    cuerpo = json.loads(aviso["Body"])
    assert json.loads(proyeccion["Body"]) == cuerpo
    _validar(cuerpo)
    assert (cuerpo["type"], cuerpo["correlationId"]) == ("PolizaEmitida", CORRELACION)
    payload = cuerpo["payload"]
    assert payload["polizaId"] == str(poliza["poliza_id"])
    assert payload["claveRecaudo"] == str(clave) and payload["clienteId"] == C01
    assert payload["numero"] == poliza["numero"]
    documento = payload["documento"]
    assert documento == {
        "clave": poliza["documento_clave"],
        "versionId": poliza["documento_version_id"],
        "huella": poliza["documento_huella"],
        "tamanoBytes": poliza["documento_tamano_bytes"],
    }
    assert payload["firma"]["firmaId"] == poliza["firma_id"] == "FIR-SINT-C01"
    # HU-W17 (17.8b): las coberturas de la instantánea de la decisión aceptada (S01), nunca del
    # catálogo, y el ramo de la póliza.
    assert payload["coberturas"] == [
        {"codigo": c.codigo, "nombre": c.nombre, "obligatoria": c.obligatoria}
        for c in sinteticos.insumos("S01").coberturas
    ]
    assert len(payload["coberturas"]) == 2
    assert payload["ramo"] == {"codigo": "100", "nombre": "Vida hipotecario"}
    assert (poliza["ramo_codigo"], poliza["ramo_nombre"]) == ("100", "Vida hipotecario")
    # Una sola versión del documento en el bucket, la referenciada.
    assert _versiones(localstack, bucket, documento["clave"]) == [documento["versionId"]]

    # La URL prefirmada de la versión registrada descarga el PDF cuya huella es la del evento.
    almacen = AlmacenDocumentosS3(cliente_s3(REGION, endpoint_url=localstack), bucket, "AES256")
    url = await almacen.url_descarga(documento["clave"], documento["versionId"], 300)
    async with httpx.AsyncClient(timeout=10) as cliente:
        descarga = await cliente.get(url)
    assert descarga.status_code == 200
    assert descarga.content.startswith(b"%PDF-")
    assert _sha256(descarga.content) == documento["huella"]
    assert len(descarga.content) == documento["tamanoBytes"]

    # adaptador-firma recibió la huella registrada, la póliza como documento y la correlación.
    [pedido] = firma.firmas()
    assert json.loads(pedido["body"]) == {
        "documentoId": payload["polizaId"],
        "huella": documento["huella"],
        "firmanteRef": C01,
    }
    assert pedido["headers"]["X-Correlation-Id"] == CORRELACION


async def test_un_recaudo_rechazado_no_produce_poliza(
    abrir_api_emisora: Any,
    cola_recaudos: Any,
    firma: Any,
    sinteticos: Any,
    limpia: str,
    localstack: str,
    bucket: str,
) -> None:
    async with abrir_api_emisora() as api:
        clave = await _aceptar(api, sinteticos)
        await asyncio.to_thread(cola_recaudos.publicar, _sobre("RecaudoRechazado", clave))
        await _esperar(lambda: _filas(limpia, "SELECT 1 FROM recaudos_aceptacion"))
        # Varios ciclos de la tarea emisora (cada 0,1 s).
        await asyncio.sleep(1)
    assert _filas(limpia, "SELECT 1 FROM polizas") == []
    assert _emitidas(limpia) == []
    assert firma.firmas() == []
    s3 = boto3.client("s3", region_name=REGION, endpoint_url=localstack)
    assert s3.list_object_versions(Bucket=bucket).get("Versions", []) == []


# --- Reintentos con la misma versión ----------------------------------------------------------
async def test_firma_lenta_deja_emitiendo_y_el_reintento_activa_con_la_misma_version(
    abrir_api_emisora: Any,
    cola_recaudos: Any,
    firma: Any,
    sinteticos: Any,
    limpia: str,
    localstack: str,
    bucket: str,
    dobles: Any,
) -> None:
    firma.modo("lento")
    async with abrir_api_emisora() as api:
        clave = await _aceptar(api, sinteticos)
        await asyncio.to_thread(cola_recaudos.publicar, _sobre("RecaudoConfirmado", clave))

        async def fallida() -> bool:
            filas = _filas(limpia, "SELECT ultimo_fallo FROM polizas")
            return bool(filas) and filas[0]["ultimo_fallo"] is not None

        await _esperar(fallida, limite_s=40)
        pendiente = _poliza(limpia, clave)
        assert (pendiente["estado"], pendiente["ultimo_fallo"]) == (
            "EMITIENDO",
            "FIRMA_TIEMPO_AGOTADO",
        )
        assert pendiente["documento_version_id"] is not None and pendiente["firma_id"] is None
        # Antes de reintento_s (30 s) no se reintenta.
        await asyncio.sleep(1)
        assert len(firma.firmas()) == 1

        firma.modo("firmado")
        dobles.reloj.instante += timedelta(seconds=31)
        await _esperar(lambda: _poliza(limpia, clave)["estado"] == "ACTIVA")

    activa = _poliza(limpia, clave)
    assert activa["documento_version_id"] == pendiente["documento_version_id"]
    assert activa["documento_huella"] == pendiente["documento_huella"]
    assert _versiones(localstack, bucket, activa["documento_clave"]) == [
        activa["documento_version_id"]
    ]
    # Las dos firmas, de la misma huella registrada.
    huellas = [json.loads(p["body"])["huella"] for p in firma.firmas()]
    assert huellas == [activa["documento_huella"]] * 2
    [evento] = _emitidas(limpia)
    assert evento["payload"]["documento"]["versionId"] == activa["documento_version_id"]


async def test_s3_caido_deja_emitiendo_y_se_recupera(
    api: Any,
    firma: Any,
    sinteticos: Any,
    limpia: str,
    localstack: str,
    bucket: str,
    dobles: Any,
    credenciales: None,
) -> None:
    clave = await _aceptar(api, sinteticos)
    await _confirmar(limpia, dobles, clave)
    # Un puerto sin nada que escuche: sin conexión.
    async with _emisora(
        limpia, dobles, localstack, bucket, firma.url, endpoint_s3="http://127.0.0.1:9"
    ) as caida:
        [emision] = await caida.emitir_una_vez()
    assert (emision.resultado, emision.codigo) == (Resultado.PENDIENTE, "ALMACEN_NO_DISPONIBLE")
    pendiente = _poliza(limpia, clave)
    assert (pendiente["estado"], pendiente["documento_clave"]) == ("EMITIENDO", None)
    assert firma.firmas() == []

    dobles.reloj.instante += timedelta(seconds=31)
    async with _emisora(limpia, dobles, localstack, bucket, firma.url) as sana:
        [emision] = await sana.emitir_una_vez()
    assert emision.resultado is Resultado.OK
    activa = _poliza(limpia, clave)
    assert activa["estado"] == "ACTIVA"
    assert _versiones(localstack, bucket, activa["documento_clave"]) == [
        activa["documento_version_id"]
    ]
    assert len(_emitidas(limpia)) == 1


async def test_una_caida_tras_registrar_el_documento_no_sube_otro(
    api: Any,
    firma: Any,
    sinteticos: Any,
    limpia: str,
    localstack: str,
    bucket: str,
    dobles: Any,
    credenciales: None,
) -> None:
    clave = await _aceptar(api, sinteticos)
    await _confirmar(limpia, dobles, clave)
    firma.modo("lento")
    async with _emisora(limpia, dobles, localstack, bucket, firma.url) as primera:
        tarea = asyncio.create_task(primera.emitir_una_vez())
        await _esperar(lambda: _poliza(limpia, clave)["documento_clave"] is not None)
        # La tarea cae durante la firma (SIGKILL, o la parada que vence).
        tarea.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarea
    caida = _poliza(limpia, clave)
    assert (caida["estado"], caida["ultimo_fallo"], caida["firma_id"]) == ("EMITIENDO", None, None)
    # 17.8 (ajuste): una cancelación (la parada que vence) libera la reserva al salir; la caída
    # sin liberar (SIGKILL) es test_una_caida_entre_la_subida_y_el_registro_….
    assert caida["reservada_hasta"] is None

    firma.modo("firmado")
    # Sin reserva vigente ni fallo, se retoma en el ciclo siguiente, sin subir otro documento.
    async with _emisora(limpia, dobles, localstack, bucket, firma.url) as segunda:
        [emision] = await segunda.emitir_una_vez()
    assert emision.resultado is Resultado.OK
    activa = _poliza(limpia, clave)
    assert activa["documento_version_id"] == caida["documento_version_id"]
    assert _versiones(localstack, bucket, activa["documento_clave"]) == [
        caida["documento_version_id"]
    ]
    [evento] = _emitidas(limpia)
    assert evento["payload"]["documento"]["versionId"] == caida["documento_version_id"]


# --- Concurrencia y reserva (17.8, ajuste) ---------------------------------------------------
async def test_dos_tareas_sobre_la_misma_poliza_nueva_una_version_una_firma_y_un_evento(
    api: Any,
    firma: Any,
    sinteticos: Any,
    limpia: str,
    localstack: str,
    bucket: str,
    dobles: Any,
    credenciales: None,
) -> None:
    """El bucket expira las versiones no actuales (infra/platform/storage.tf:132-133, 90 días): si
    dos tareas suben la misma clave, la registrada puede dejar de ser la actual. Con la reserva,
    solo una procesa la póliza."""
    clave = await _aceptar(api, sinteticos)
    await _confirmar(limpia, dobles, clave)
    # Con retardo, las dos tareas leen la misma póliza nueva y la quieren a la vez.
    firma.modo("firmado", retardo_ms=300)
    async with (
        _emisora(limpia, dobles, localstack, bucket, firma.url) as una,
        _emisora(limpia, dobles, localstack, bucket, firma.url) as otra,
    ):
        resultados = await asyncio.gather(una.emitir_una_vez(), otra.emitir_una_vez())

    poliza = _poliza(limpia, clave)
    # Una sola versión de la clave en el bucket (la registrada es la actual) y una sola firma.
    assert _versiones(localstack, bucket, poliza["documento_clave"]) == [
        poliza["documento_version_id"]
    ]
    assert len(firma.firmas()) == 1
    assert (poliza["estado"], poliza["reservada_hasta"]) == ("ACTIVA", None)
    # Solo una la procesó (la otra no obtuvo la reserva y no la mide).
    emisiones = [e for lista in resultados for e in lista]
    assert [e.resultado for e in emisiones] == [Resultado.OK]
    [evento] = _emitidas(limpia)
    assert evento["payload"]["documento"]["versionId"] == poliza["documento_version_id"]


async def test_una_caida_entre_la_subida_y_el_registro_reintenta_al_vencer_la_reserva(
    api: Any,
    firma: Any,
    sinteticos: Any,
    limpia: str,
    localstack: str,
    bucket: str,
    dobles: Any,
    credenciales: None,
) -> None:
    """Una tarea reservó la póliza, subió su PDF y cayó (SIGKILL) antes de registrarlo: la reserva
    queda hasta vencer. Después, el reintento sube otra versión y registra la actual."""
    from polizas.adapters.outbound.polizas_pg import TransaccionesPg
    from polizas.domain.poliza.documento import clave_documento
    from psycopg_pool import AsyncConnectionPool

    clave = await _aceptar(api, sinteticos)
    await _confirmar(limpia, dobles, clave)
    ahora = dobles.reloj.ahora()
    async with (
        AsyncConnectionPool(limpia, min_size=1, max_size=2, open=False) as pool,
        TransaccionesPg(pool).transaccion() as tx,
    ):
        reservada = await PolizasPg().reservar(
            _poliza(limpia, clave)["poliza_id"], ahora, ahora + timedelta(seconds=60), tx
        )
    assert reservada is not None
    # El disparador de 004 deja cambiar reservada_hasta en una póliza EMITIENDO.
    assert _poliza(limpia, clave)["reservada_hasta"] == ahora + timedelta(seconds=60)
    almacen = AlmacenDocumentosS3(cliente_s3(REGION, endpoint_url=localstack), bucket, "AES256")
    huerfana = await almacen.guardar(clave_documento(reservada), b"%PDF-de la tarea caida")

    async with _emisora(limpia, dobles, localstack, bucket, firma.url) as emisora:
        # Con la reserva vigente nadie la toma.
        assert await emisora.emitir_una_vez() == []
        dobles.reloj.instante = ahora + timedelta(seconds=61)
        [emision] = await emisora.emitir_una_vez()
    assert emision.resultado is Resultado.OK

    activa = _poliza(limpia, clave)
    assert (activa["estado"], activa["reservada_hasta"]) == ("ACTIVA", None)
    s3 = boto3.client("s3", region_name=REGION, endpoint_url=localstack)
    versiones = s3.list_object_versions(Bucket=bucket, Prefix=activa["documento_clave"])["Versions"]
    assert len(versiones) == 2
    [actual] = [v["VersionId"] for v in versiones if v["IsLatest"]]
    # La registrada es la actual, no la huérfana (que el ciclo de vida expira).
    assert activa["documento_version_id"] == actual != huerfana
    [evento] = _emitidas(limpia)
    assert evento["payload"]["documento"]["versionId"] == actual


# --- Vencida ---------------------------------------------------------------------------------
async def test_una_pendiente_de_mas_de_una_hora_se_mide_como_error(
    api: Any,
    firma: Any,
    sinteticos: Any,
    limpia: str,
    localstack: str,
    bucket: str,
    dobles: Any,
    credenciales: None,
) -> None:
    clave = await _aceptar(api, sinteticos)
    await _confirmar(limpia, dobles, clave)
    firma.modo("caido")
    dobles.reloj.instante += timedelta(seconds=3601)
    async with _emisora(limpia, dobles, localstack, bucket, firma.url) as emisora:
        [emision] = await emisora.emitir_una_vez()
    assert (emision.resultado, emision.codigo, emision.vencida) == (
        Resultado.ERROR,
        "FIRMA_NO_DISPONIBLE",
        True,
    )
    pendiente = _poliza(limpia, clave)
    assert (pendiente["estado"], pendiente["ultimo_fallo"]) == ("EMITIENDO", "FIRMA_NO_DISPONIBLE")
    assert _emitidas(limpia) == []


# --- Migración --------------------------------------------------------------------------------
async def test_migracion_005_adelante_atras_y_adelante(base: str) -> None:
    reversa = MIGRACIONES.joinpath("down", "005_emision.sql").read_text(encoding="utf-8")
    with psycopg.connect(base, autocommit=True) as conexion:
        conexion.execute(reversa)
        conexion.execute(reversa)
    columna = (
        "SELECT count(*) AS n FROM information_schema.columns "
        "WHERE table_name = 'polizas' AND column_name = 'correlation_id'"
    )
    indice = "SELECT count(*) AS n FROM pg_indexes WHERE indexname = 'polizas_emitiendo'"
    reserva = columna.replace("'correlation_id'", "'reservada_hasta'")
    assert _filas(base, columna) == [{"n": 0}] and _filas(base, indice) == [{"n": 0}]
    assert _filas(base, reserva) == [{"n": 0}]
    # 004 sigue: la tabla de pólizas no se toca.
    assert _filas(base, "SELECT to_regclass('polizas') IS NOT NULL AS existe") == [{"existe": True}]
    migrar(base)
    migrar(base)
    assert _filas(base, columna) == [{"n": 1}] and _filas(base, indice) == [{"n": 1}]
    assert _filas(base, reserva) == [{"n": 1}]
    [definicion] = _filas(
        base, "SELECT indexdef FROM pg_indexes WHERE indexname = 'polizas_emitiendo'"
    )
    assert "WHERE (estado = 'EMITIENDO'::text)" in definicion["indexdef"]


async def test_la_correlacion_rechaza_una_forma_invalida(
    api: Any, sinteticos: Any, limpia: str, dobles: Any
) -> None:
    clave = await _aceptar(api, sinteticos)
    from psycopg_pool import AsyncConnectionPool

    async with AsyncConnectionPool(limpia, min_size=1, max_size=2, open=False) as pool:
        inbox = InboxPg(pool)
        caso = ProyectarRecaudo(RecaudosPg(pool), PolizasPg(), dobles.reloj)
        with pytest.raises(psycopg.errors.CheckViolation, match="polizas_correlation_id"):
            async with inbox.transaccion() as tx:
                await caso.ejecutar(_resultado(clave), tx, "con espacios")
