"""N3 R5 (HU-W24) contra un ambiente desplegado: particiones, auditoría por lotes y cola de descarte.

Hace por el tópico de eventos y por el portal lo que las pruebas de módulo de HU-W24 ya verificaron con
LocalStack (`libs/solventa-common/tests/module/test_hu_w24_particiones.py` y las de `auditoria` y
`pagos-parametricos`):

- C01 (AC1, AC4 y D3): una ráfaga de `E2E_W24_EVENTOS` (30) sobres `HumoAuditoria`, cada uno publicado
  **dos veces**, llega a `auditoria` (la única cola sin filtro) **una vez** por `eventId`, y los lotes
  `cadena/*.json` escritos desde el inicio de la prueba forman una sola cadena que verifica
  (`previousHash`, secuencia y huella de cada registro). `HumoAuditoria` no lo filtra ninguna otra
  cola, así que no ensucia los grupos de negocio (como el paso 13 de `scripts/plataforma/smoke.sh`).
- C02 (AC2 y AC3): un `EventoParametricoObservado` envenenado (el caso `envenenadoSinZona` de
  `contracts/datos-sinteticos/eventos-parametricos.json`, con un `eventId` nuevo) llega a `auditoria`
  mientras el grupo de `pagos-parametricos` lo reintenta: un grupo con fallos no frena a los demás.
  Tras 5 recepciones pasa a la DLQ y H10 (`/web/v1/siniestros/parametricos/descartes`) lo muestra con
  causa `FORMATO_INVALIDO`, sus intentos y la retención de la DLQ (14 días); listar **no lo consume**.
- C03 (H11): el detalle trae la clave de idempotencia (= `eventId`); descartarlo con motivo responde
  200, repetirlo da la misma resolución (idempotente), corregirlo después es 409
  `DESCARTE_YA_RESUELTO`, ya no está en H10 y `auditoria` recibe **un solo**
  `DescarteParametricoResuelto` con el analista como actor. Se descarta, no se reprocesa: un reproceso
  corregido evaluaría el evento y podría crear un siniestro de verdad (la reentrega con el mismo
  `eventId` la cubre la prueba de módulo de `pagos-parametricos`).

El **grupo caído** (AC2 con un consumidor detenido) y el **desorden** de `PolizaEmitida`/`PolizaCancelada`
(AC4) no se provocan en un ambiente compartido: los cubren las N2 de HU-W24 y de HU-W21.

Requiere (sin ellos se omite, como las demás N3):

- `E2E_PUBLICAR_W24=1`: **escribe en el tópico de eventos de INT** (30 × 2 sobres `HumoAuditoria` y un
  `EventoParametricoObservado` envenenado, que queda en la DLQ hasta que C03 lo descarta).
- Credenciales de AWS con `sns:Publish` y lectura del bucket de auditoría, y las salidas
  `events_topic_arn` y `audit_bucket` de `infra/platform`.
- C02 y C03: el usuario E2E del back-office (`E2E_ADMIN_*` o `E2E_ADMIN_TOKEN`) en el grupo
  `analistas-siniestros`, igual que HU-W19 y HU-W22.

Espera a la auditoría `E2E_AUDITORIA_ESPERA_S` (180 s) y a la DLQ `E2E_DLQ_ESPERA_S` (600 s: cinco
recepciones con la visibilidad de la cola y la espera larga).
"""

import json
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from auditoria.domain.registro import verificar_cadena

HISTORIA = "HU-W24 (R5)"
RAIZ = Path(__file__).parents[2]
DATOS = RAIZ / "contracts/datos-sinteticos/eventos-parametricos.json"
EVENTOS = int(os.getenv("E2E_W24_EVENTOS", "30"))
ESPERA_AUDITORIA_S = int(os.getenv("E2E_AUDITORIA_ESPERA_S", "180"))
ESPERA_DLQ_S = int(os.getenv("E2E_DLQ_ESPERA_S", "600"))
TIMEOUT_S = 30
HUMO = "HumoAuditoria"
OBSERVADO = "EventoParametricoObservado"
RESUELTO = "DescarteParametricoResuelto"
MOTIVO = "N3 HU-W24: evento sintético sin zona"
# Lo que C02 deja para C03.
ENVENENADO: dict[str, Any] = {}


class Auditoria:
    """El tópico de eventos de negocio y los lotes `cadena/*.json` del bucket de auditoría."""

    def __init__(self, salidas: dict[str, Any]) -> None:
        import boto3

        self.bucket: str = salidas["audit_bucket"]["name"]
        self.topico: str = salidas["events_topic_arn"]
        self._s3 = boto3.client("s3")
        self._sns = boto3.client("sns")
        self.desde = datetime.now(UTC)

    def publicar(self, cuerpo: dict[str, Any]) -> None:
        self._sns.publish(
            TopicArn=self.topico,
            Message=json.dumps(cuerpo),
            MessageAttributes={"eventType": {"DataType": "String", "StringValue": cuerpo["type"]}},
        )

    def lotes(self) -> list[dict[str, Any]]:
        """Los documentos de lote escritos desde que empezó la prueba, en orden de secuencia."""
        encontrados: list[dict[str, Any]] = []
        paginas = self._s3.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix="cadena/"
        )
        for pagina in paginas:
            for objeto in pagina.get("Contents", []):
                if objeto["LastModified"] < self.desde:
                    continue
                cuerpo = self._s3.get_object(Bucket=self.bucket, Key=objeto["Key"])["Body"].read()
                encontrados.append(json.loads(cuerpo))
        return sorted(encontrados, key=lambda lote: int(lote["secuencia"]))

    def registros(self) -> list[dict[str, Any]]:
        return [registro for lote in self.lotes() for registro in lote["registros"]]

    def esperar(self, condicion: Any, espera_s: int = ESPERA_AUDITORIA_S) -> list[dict[str, Any]]:
        limite = time.monotonic() + espera_s
        while True:
            registros = self.registros()
            if condicion(registros) or time.monotonic() > limite:
                return registros
            time.sleep(5)


class Portal:
    """H10 y H11 por `/web`, con el usuario E2E del back-office (grupo `analistas-siniestros`)."""

    def __init__(self, base: str, token: str) -> None:
        self._base = f"{base}/web/v1/siniestros/parametricos/descartes"
        self._cabeceras = {"Authorization": f"Bearer {token}"}

    def _verificar(self, respuesta: httpx.Response) -> httpx.Response:
        assert respuesta.status_code != 403, (
            "el usuario E2E del back-office debe estar en el grupo analistas-siniestros "
            "(infra/DESPLIEGUE.md)"
        )
        return respuesta

    def get(self, ruta: str = "") -> httpx.Response:
        return self._verificar(
            httpx.get(f"{self._base}{ruta}", headers=self._cabeceras, timeout=TIMEOUT_S)
        )

    def post(self, ruta: str, cuerpo: dict[str, Any]) -> httpx.Response:
        return self._verificar(
            httpx.post(
                f"{self._base}{ruta}", json=cuerpo, headers=self._cabeceras, timeout=TIMEOUT_S
            )
        )

    def listado(self) -> dict[str, Any]:
        respuesta = self.get()
        assert respuesta.status_code == 200, respuesta.text
        cuerpo: dict[str, Any] = respuesta.json()
        return cuerpo

    def del_evento(self, evento_id: str) -> list[dict[str, Any]]:
        return [m for m in self.listado()["mensajes"] if m["eventoId"] == evento_id]

    def esperar_en_dlq(self, evento_id: str) -> list[dict[str, Any]]:
        limite = time.monotonic() + ESPERA_DLQ_S
        while True:
            encontrados = self.del_evento(evento_id)
            if encontrados or time.monotonic() > limite:
                return encontrados
            time.sleep(15)


@pytest.fixture(scope="module")
def auditoria(ambiente: Any) -> Auditoria:
    if os.getenv("E2E_PUBLICAR_W24") != "1":
        pytest.skip(
            "N3 HU-W24: falta E2E_PUBLICAR_W24=1 (publica sobres sintéticos en el tópico de eventos "
            "de INT)"
        )
    salidas = ambiente.salidas.get("platform", {})  # las dos salidas viven en el objeto `platform`
    if not (salidas.get("audit_bucket", {}).get("name") and salidas.get("events_topic_arn")):
        pytest.skip(
            "N3 HU-W24: faltan las salidas audit_bucket y events_topic_arn de infra/platform"
        )
    try:
        return Auditoria(salidas)
    except Exception as error:  # noqa: BLE001 - sin credenciales de AWS no hay tópico ni auditoría
        pytest.skip(f"N3 HU-W24: sin acceso a AWS ({type(error).__name__})")


@pytest.fixture(scope="module")
def portal(ambiente: Any) -> Portal:
    config = ambiente.requerir(
        HISTORIA, {"canales": ambiente.urls["canales"], "token": ambiente.token_admin()}
    )
    return Portal(config["canales"], config["token"])


def _humo(evento_id: str) -> dict[str, Any]:
    return {
        "eventId": evento_id,
        "aggregateId": evento_id,
        "type": HUMO,
        "version": 1,
        "occurredAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "correlationId": f"e2e-w24-{evento_id}",
        "sintetico": True,
        "payload": {},
    }


def _envenenado() -> dict[str, Any]:
    """`envenenadoSinZona` con un `eventId` nuevo: cada ejecución deja su propio mensaje en la DLQ."""
    datos = json.loads(DATOS.read_text(encoding="utf-8"))
    (caso,) = [m for m in datos["mensajesCola"] if m["clave"] == "envenenadoSinZona"]
    evento_id = str(uuid.uuid4())
    sobre: dict[str, Any] = json.loads(json.dumps(caso["sobre"]))
    sobre.update(eventId=evento_id, aggregateId=evento_id, correlationId=f"e2e-w24-{evento_id}")
    sobre["payload"].update(eventoId=evento_id, idExterno=f"E2E-W24-{evento_id[:8]}")
    assert "zona" not in sobre["payload"]
    return sobre


def test_hu_w24_r5_c01_la_rafaga_con_duplicados_queda_una_vez_en_una_cadena_que_verifica(
    auditoria: Auditoria,
) -> None:
    ids = [str(uuid.uuid4()) for _ in range(EVENTOS)]
    for _ in range(2):  # cada sobre dos veces: la entrega repetida no repite el efecto (AC4)
        for evento_id in ids:
            auditoria.publicar(_humo(evento_id))

    registros = auditoria.esperar(lambda r: set(ids) <= {x["eventId"] for x in r})

    propios = [x["eventId"] for x in registros if x["eventId"] in set(ids)]
    faltan = set(ids) - set(propios)
    assert not faltan, f"{len(faltan)} de {EVENTOS} eventos no llegaron a auditoría"
    repetidos = {e for e in propios if propios.count(e) > 1}
    assert not repetidos, f"{len(repetidos)} eventos registrados más de una vez"
    # D3: los lotes desde el inicio enlazan entre sí y cada registro conserva su huella.
    lotes = auditoria.lotes()
    verificar_cadena(lotes, desde=lotes[0]["previousHash"])
    assert all(x["accion"] == HUMO for x in registros if x["eventId"] in set(ids))


def test_hu_w24_r5_c02_el_envenenado_no_frena_auditoria_y_llega_a_h10_con_su_causa(
    auditoria: Auditoria, portal: Portal
) -> None:
    sobre = _envenenado()
    evento_id = sobre["eventId"]
    auditoria.publicar(sobre)

    # AC2: auditoria lo registra mientras pagos-parametricos todavía lo reintenta.
    registros = auditoria.esperar(lambda r: evento_id in {x["eventId"] for x in r})
    assert [x["accion"] for x in registros if x["eventId"] == evento_id] == [OBSERVADO]

    # AC3: tras 5 recepciones, a la DLQ con trazabilidad; H10 lo muestra.
    encontrados = portal.esperar_en_dlq(evento_id)
    assert len(encontrados) == 1, (
        f"el envenenado no llegó a H10 en {ESPERA_DLQ_S} s (E2E_DLQ_ESPERA_S)"
    )
    (mensaje,) = encontrados
    assert mensaje["tipo"] == OBSERVADO
    assert mensaje["causa"] == "FORMATO_INVALIDO", mensaje
    assert mensaje["intentos"] >= 5
    listado = portal.listado()
    assert (listado["retencionDias"], listado["maxIntentos"]) == (14, 5)
    # Listar no consume: sigue ahí en la siguiente lectura.
    assert [m["messageId"] for m in portal.del_evento(evento_id)] == [mensaje["messageId"]]
    ENVENENADO.update(evento_id=evento_id, message_id=mensaje["messageId"])


def test_hu_w24_r5_c03_descartar_es_idempotente_auditado_y_excluye_otra_accion(
    auditoria: Auditoria, portal: Portal
) -> None:
    assert ENVENENADO, "C02 debe correr antes que C03"
    message_id, evento_id = ENVENENADO["message_id"], ENVENENADO["evento_id"]

    detalle = portal.get(f"/{message_id}")
    assert detalle.status_code == 200, detalle.text
    assert detalle.json()["claveIdempotencia"] == evento_id

    primera = portal.post(f"/{message_id}/descarte", {"motivo": MOTIVO})
    assert primera.status_code == 200, primera.text
    resolucion = primera.json()
    assert (resolucion["accion"], resolucion["eventoId"]) == ("DESCARTADO", evento_id)

    repetida = portal.post(f"/{message_id}/descarte", {"motivo": MOTIVO})
    assert (repetida.status_code, repetida.json()) == (200, resolucion)

    otra = portal.post(f"/{message_id}/correccion", {"cambios": {"zona": "11001"}})
    assert (otra.status_code, otra.json()["codigo"]) == (409, "DESCARTE_YA_RESUELTO")

    assert portal.del_evento(evento_id) == []

    registros = auditoria.esperar(
        lambda r: any(x["accion"] == RESUELTO and x["resourceId"] == evento_id for x in r)
    )
    resueltos = [x for x in registros if x["accion"] == RESUELTO and x["resourceId"] == evento_id]
    assert len(resueltos) == 1, f"{len(resueltos)} DescarteParametricoResuelto en auditoría"
    assert resueltos[0]["actor"] == resolucion["actorId"]
