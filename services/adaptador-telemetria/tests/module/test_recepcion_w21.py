"""MT-W21-01 (N2): adaptador-telemetria con PostgreSQL y LocalStack S3, SNS y SQS (HU-W21).

El archivo del original, el Outbox, el duplicado y los rechazos sin efectos, con los casos de
`contracts/datos-sinteticos/eventos-parametricos.json`; el relay hacia el tópico; la migración; y
que ni el secreto, ni la firma, ni el cuerpo lleguen a los logs (SEC-W21-01).
"""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from adaptador_telemetria.adapters.outbound.base_de_datos import migrar
from adaptador_telemetria.domain.telemetria.firma import calcular_firma
from jsonschema import Draft202012Validator

pytestmark = pytest.mark.anyio
EVENTOS = Path(__file__).parents[4] / "contracts" / "events"
MIGRACIONES = Path(__file__).parents[2] / "src/adaptador_telemetria/adapters/outbound/migraciones"


def _ahora() -> datetime:
    return datetime.now(UTC)


async def _enviar(servicio: Any, envio: Any) -> Any:
    return await servicio.cliente.post(
        "/v1/eventos", content=envio.cuerpo, headers=envio.encabezados()
    )


def _filas(sql: Callable[..., Any], base: str, tabla: str) -> int:
    return int(sql(base, f"SELECT count(*) FROM {tabla}")[0][0])


def _esquema() -> Draft202012Validator:
    esquema = json.loads(
        (EVENTOS / "EventoParametricoObservado.v1.schema.json").read_text(encoding="utf-8")
    )
    return Draft202012Validator(esquema, format_checker=Draft202012Validator.FORMAT_CHECKER)


async def test_un_evento_valido_queda_archivado_registrado_y_en_el_outbox(
    servicio: Callable[..., Any],
    bucket: Any,
    base: str,
    sql: Callable[..., Any],
    casos: dict[str, dict[str, Any]],
    firmar_caso: Callable[..., Any],
) -> None:
    envio = firmar_caso(casos["valido"], _ahora())
    async with servicio() as s:
        respuesta = await _enviar(s, envio)
    assert respuesta.status_code == 202
    evento_id = respuesta.json()["eventoId"]
    assert evento_id == casos["valido"]["esperado"]["eventoId"]

    # El original, byte por byte, cifrado como exige la política del bucket y con su checksum.
    [clave] = bucket.claves()
    fecha = _ahora().strftime("%Y/%m/%d")
    assert clave == f"telemetria/{fecha}/{evento_id}.json"
    assert bucket.leer(clave) == envio.cuerpo
    cabecera = bucket.cabecera(clave)
    assert cabecera["ServerSideEncryption"] == "AES256"
    assert cabecera["ChecksumSHA256"]
    assert cabecera["ContentType"] == "application/json"
    assert cabecera["Metadata"]["fuente"] == "IDEAM-SIM"
    assert cabecera["Metadata"]["evento-id"] == evento_id
    assert cabecera["Metadata"]["id-externo"] == "IDEAM-2026-1110-0001"

    # El registro y una sola fila de Outbox, sin publicar (el relay está apagado).
    [fila] = sql(base, "SELECT evento_id, fuente, id_externo, archivo_ref FROM eventos_recibidos")
    assert (str(fila[0]), fila[1], fila[2], fila[3]) == (
        evento_id,
        "IDEAM-SIM",
        "IDEAM-2026-1110-0001",
        clave,
    )
    [(event_id, tipo, version, publicado, payload)] = sql(
        base, "SELECT event_id, event_type, event_version, published_at, payload FROM outbox"
    )
    assert (str(event_id), tipo, version, publicado) == (
        evento_id,
        "EventoParametricoObservado",
        1,
        None,
    )
    assert payload["archivoRef"] == clave
    assert payload["valorObservado"] == "135" and "ubicacion" not in payload


async def test_cada_caso_de_la_matriz_responde_lo_esperado_y_los_rechazos_no_dejan_rastro(
    servicio: Callable[..., Any],
    bucket: Any,
    base: str,
    sql: Callable[..., Any],
    datos: dict[str, Any],
    firmar_caso: Callable[..., Any],
) -> None:
    primero: dict[str, Any] = {}
    async with servicio() as s:
        for caso in datos["eventos"]:
            esperado = caso["esperado"]
            claves_antes = bucket.claves()
            filas_antes = (
                _filas(sql, base, "eventos_recibidos"),
                _filas(sql, base, "outbox"),
            )
            respuesta = await _enviar(s, firmar_caso(caso, _ahora()))
            http = esperado["http"]
            assert respuesta.status_code == http["estado"], caso["clave"]
            cuerpo = respuesta.json()
            if http["codigo"]:
                # Un rechazo: ni objeto, ni registro, ni Outbox.
                assert cuerpo["codigo"] == http["codigo"], caso["clave"]
                assert bucket.claves() == claves_antes, caso["clave"]
                assert (
                    _filas(sql, base, "eventos_recibidos"),
                    _filas(sql, base, "outbox"),
                ) == filas_antes, caso["clave"]
            else:
                assert cuerpo["eventoId"] == esperado["eventoId"], caso["clave"]
                nuevas = _filas(sql, base, "outbox") - filas_antes[1]
                assert nuevas == esperado["outboxNuevas"], caso["clave"]
                if http["estado"] == 202:
                    primero[caso["clave"]] = cuerpo
                    assert len(bucket.claves()) == len(claves_antes) + 1
                else:
                    # El reenvío: el mismo evento y el mismo instante del primero; el original no cambia.
                    assert cuerpo == primero[caso["reenvioDe"]]
                    assert bucket.claves() == claves_antes
    aceptados = [c for c in datos["eventos"] if c["esperado"]["http"]["estado"] == 202]
    assert len(aceptados) == 8
    assert _filas(sql, base, "eventos_recibidos") == _filas(sql, base, "outbox") == 8
    assert len(bucket.claves()) == 8


async def test_el_reenvio_con_otro_formato_no_pisa_el_original(
    servicio: Callable[..., Any],
    bucket: Any,
    base: str,
    sql: Callable[..., Any],
    casos: dict[str, dict[str, Any]],
    firmar_caso: Callable[..., Any],
    secreto: str,
) -> None:
    primero = firmar_caso(casos["valido"], _ahora())
    reformateado = json.dumps(json.loads(primero.cuerpo), indent=4).encode()
    marca = _ahora().isoformat().replace("+00:00", "Z")
    async with servicio() as s:
        a = await _enviar(s, primero)
        b = await s.cliente.post(
            "/v1/eventos",
            content=reformateado,
            headers={
                "X-Telemetria-Fuente": "IDEAM-SIM",
                "X-Telemetria-Marca-Tiempo": marca,
                "X-Telemetria-Firma": calcular_firma(secreto, marca, reformateado),
            },
        )
    assert (a.status_code, b.status_code) == (202, 200)
    assert b.json() == a.json()
    [clave] = bucket.claves()
    assert bucket.leer(clave) == primero.cuerpo
    assert _filas(sql, base, "outbox") == 1


async def test_diez_envios_simultaneos_del_mismo_evento_dejan_una_sola_fila(
    servicio: Callable[..., Any],
    bucket: Any,
    base: str,
    sql: Callable[..., Any],
    casos: dict[str, dict[str, Any]],
    firmar_caso: Callable[..., Any],
) -> None:
    async with servicio() as s:
        respuestas = await asyncio.gather(
            *(_enviar(s, firmar_caso(casos["valido"], _ahora())) for _ in range(10))
        )
    estados = sorted(r.status_code for r in respuestas)
    assert estados == [200] * 9 + [202]
    assert len({r.json()["eventoId"] for r in respuestas}) == 1
    assert len({r.json()["recibidoEn"] for r in respuestas}) == 1
    assert _filas(sql, base, "eventos_recibidos") == _filas(sql, base, "outbox") == 1
    assert len(bucket.claves()) == 1


async def test_si_s3_no_responde_es_503_sin_registro_y_el_reenvio_se_acepta(
    servicio: Callable[..., Any],
    bucket: Any,
    base: str,
    sql: Callable[..., Any],
    casos: dict[str, dict[str, Any]],
    firmar_caso: Callable[..., Any],
) -> None:
    envio = firmar_caso(casos["valido"], _ahora())
    async with servicio(EVIDENCIAS_BUCKET="bucket-que-no-existe") as s:
        respuesta = await _enviar(s, envio)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (503, "DEPENDENCIA_NO_DISPONIBLE")
    assert _filas(sql, base, "eventos_recibidos") == _filas(sql, base, "outbox") == 0
    assert bucket.claves() == []
    # Con el bucket de vuelta, la fuente reenvía el mismo envío y se acepta.
    async with servicio() as s:
        assert (await _enviar(s, envio)).status_code == 202
    assert len(bucket.claves()) == 1


async def test_si_la_base_falla_es_503_y_no_se_publica(
    servicio: Callable[..., Any],
    bucket: Any,
    base: str,
    sql: Callable[..., Any],
    casos: dict[str, dict[str, Any]],
    firmar_caso: Callable[..., Any],
) -> None:
    """El original ya se archivó (es idempotente); el reenvío completa el registro."""
    envio = firmar_caso(casos["valido"], _ahora())
    # Un disparador que rechaza la fila del Outbox: la base falla a mitad de la transacción.
    sql(
        base,
        "CREATE OR REPLACE FUNCTION falla_outbox() RETURNS trigger LANGUAGE plpgsql AS "
        "$$BEGIN RAISE EXCEPTION 'fallo de prueba'; END$$",
    )
    sql(
        base,
        "CREATE TRIGGER falla BEFORE INSERT ON outbox FOR EACH ROW EXECUTE FUNCTION falla_outbox()",
    )
    try:
        async with servicio() as s:
            respuesta = await _enviar(s, envio)
    finally:
        sql(base, "DROP TRIGGER falla ON outbox")
    assert respuesta.status_code == 503
    # La transacción se deshizo entera: ni registro ni Outbox.
    assert _filas(sql, base, "eventos_recibidos") == _filas(sql, base, "outbox") == 0
    async with servicio() as s:
        assert (await _enviar(s, envio)).status_code == 202
    assert _filas(sql, base, "eventos_recibidos") == _filas(sql, base, "outbox") == 1
    assert len(bucket.claves()) == 1


async def test_el_relay_publica_el_evento_en_el_topico_con_su_tipo(
    servicio: Callable[..., Any],
    localstack: str,
    base: str,
    sql: Callable[..., Any],
    casos: dict[str, dict[str, Any]],
    firmar_caso: Callable[..., Any],
) -> None:
    import boto3

    sns = boto3.client("sns", region_name="us-east-1", endpoint_url=localstack)
    sqs = boto3.client("sqs", region_name="us-east-1", endpoint_url=localstack)
    sufijo = uuid4().hex[:8]
    topico = sns.create_topic(Name=f"eventos-{sufijo}")["TopicArn"]
    url = sqs.create_queue(QueueName=f"parametricos-eventos-{sufijo}")["QueueUrl"]
    arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]
    sns.subscribe(
        TopicArn=topico,
        Protocol="sqs",
        Endpoint=arn,
        Attributes={
            "RawMessageDelivery": "true",
            "FilterPolicy": json.dumps({"eventType": ["EventoParametricoObservado"]}),
        },
    )
    envio = firmar_caso(casos["valido"], _ahora())
    async with servicio(
        OUTBOX_ENABLED="true", EVENTS_TOPIC_ARN=topico, outbox_intervalo_s=0.2
    ) as s:
        assert (await _enviar(s, envio)).status_code == 202
        mensajes: list[dict[str, Any]] = []
        for _ in range(30):
            # En un hilo: una llamada síncrona dejaría sin bucle al relay.
            recibidos = await asyncio.to_thread(
                sqs.receive_message,
                QueueUrl=url,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=1,
                MessageAttributeNames=["All"],
            )
            mensajes += recibidos.get("Messages", [])
            if mensajes:
                break
    [mensaje] = mensajes
    sobre = json.loads(mensaje["Body"])
    assert list(_esquema().iter_errors(sobre)) == []
    assert sobre["eventId"] == sobre["aggregateId"] == casos["valido"]["esperado"]["eventoId"]
    assert mensaje["MessageAttributes"]["eventType"]["StringValue"] == "EventoParametricoObservado"
    for _ in range(20):
        [(publicado,)] = sql(base, "SELECT published_at FROM outbox")
        if publicado is not None:
            break
        await asyncio.sleep(0.2)
    assert publicado is not None


async def test_ni_el_secreto_ni_la_firma_ni_el_cuerpo_llegan_a_los_logs(
    servicio: Callable[..., Any],
    capturar_logs: Callable[..., Any],
    datos: dict[str, Any],
    firmar_caso: Callable[..., Any],
    secreto: str,
) -> None:
    envios = []
    async with servicio() as s:
        salida = capturar_logs()  # `instrumentar` reconfigura el logging raíz al crear la app
        for caso in datos["eventos"]:
            envio = firmar_caso(caso, _ahora())
            envios.append(envio)
            await _enviar(s, envio)
    texto = salida.getvalue()
    assert "telemetria: evento recibido" in texto
    assert secreto not in texto
    for envio in envios:
        if envio.firma:
            assert envio.firma not in texto
        assert envio.cuerpo.decode() not in texto
    for valor in ("IDEAM-2026-1110-0001", "EVT-2026-0913", "mucha", "contacto"):
        assert valor not in texto


def test_la_migracion_va_adelante_y_atras(base: str, sql: Callable[..., Any]) -> None:
    """Revertir 001 borra las tres tablas (es idempotente); volver a migrar las recrea."""
    reversa = (MIGRACIONES / "down" / "001_telemetria.sql").read_text(encoding="utf-8")
    existen = (
        "SELECT count(*) FROM information_schema.tables WHERE table_name IN "
        "('eventos_recibidos', 'outbox', 'inbox')"
    )
    assert sql(base, existen) == [(3,)]
    sql(base, reversa)
    sql(base, reversa)
    assert sql(base, existen) == [(0,)]
    migrar(base)
    migrar(base)
    assert sql(base, existen) == [(3,)]


def test_el_registro_no_guarda_valores_medidos_y_la_fuente_con_su_id_es_unica(
    base: str, sql: Callable[..., Any]
) -> None:
    columnas = {
        c[0]
        for c in sql(
            base,
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'eventos_recibidos'",
        )
    }
    assert columnas == {
        "evento_id",
        "fuente",
        "id_externo",
        "evento_origen",
        "archivo_ref",
        "correlation_id",
        "recibido_en",
    }
    sql(base, "TRUNCATE eventos_recibidos")
    fila = ("IDEAM-SIM", "A-1", "EVT-1", "telemetria/x.json", "c")
    sql(
        base, "INSERT INTO eventos_recibidos VALUES (%s, %s, %s, %s, %s, %s, now())", uuid4(), *fila
    )
    with pytest.raises(Exception, match="duplicate key"):
        sql(
            base,
            "INSERT INTO eventos_recibidos VALUES (%s, %s, %s, %s, %s, %s, now())",
            uuid4(),
            *fila,
        )
