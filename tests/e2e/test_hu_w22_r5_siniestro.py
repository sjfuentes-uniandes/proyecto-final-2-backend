"""N3 R5 (HU-W22) contra un ambiente desplegado: del evento firmado al siniestro paramétrico.

Hace por la entrada pública lo que la versión de módulo y el recorrido local de B3 ya verificaron con
las piezas reales: firma el evento válido de `contracts/datos-sinteticos/eventos-parametricos.json`
con `scripts/dev/firmar_evento.py`, lo envía **dos veces** a `POST /telemetria/v1/eventos` (API
Gateway, sin JWT) y mira el resultado por el portal (`/web`, usuario E2E del back-office en el grupo
`analistas-siniestros`, igual que HU-W19).

- C01: el primer envío responde 202 y el reenvío 200 con el mismo `eventoId` (HU-W21, AC4); el evento
  activa una póliza y el portal muestra **un solo** siniestro paramétrico de ese `eventoId` (H09 lista
  el pago activado y el detalle de H03 trae el bloque `evento`), también unos segundos después, por si
  una entrega repetida creara otro (AC1 y AC2).
- C02: el detalle del siniestro nace `APROBADO`, sin analista ni evidencias, con la acción
  `CREADO_AUTOMATICAMENTE` del actor `SISTEMA_PARAMETRICO`, el evento y la regla que lo activaron
  (fuente, medición, umbral, regla y versión) y la liquidación sin valor reclamado, con el valor de
  la cobertura como indemnización y el pago pendiente o ya resuelto (AC3). No se pide una decisión:
  no está en «Mi bandeja» ni en la cola sin asignar.
- C03: H09 cuenta el evento y la activación y lista el pago de ese siniestro con su estado.
- C04: `auditoria` recibió **un solo** `SiniestroAprobado` de ese siniestro (SEC-W22-01 y AC1). Lee los
  lotes `cadena/*.json` del bucket de auditoría, como HU-W21 C03.

El tramo siniestro → indemnización (R5 → R7) lo comprueba `test_hu_w23_r5_r7_indemnizacion.py`. El
**siniestros caído → reintentos → DLQ** y la recuperación no se ven desde fuera (no se apaga un
servicio de un ambiente compartido): los cubren `MT-W22-03` y el recorrido local de B3.

Requiere (sin ellos se omite, como las demás N3):

- `E2E_TELEMETRIA_SECRETO`: el `hmac_secret` de prueba del aliado `telemetria` (Secrets Manager). Lo
  entrega la persona; esta prueba no lo lee de AWS. `E2E_TELEMETRIA_URL` reemplaza la URL del borde
  (por omisión, `<canales>/telemetria/v1/eventos`).
- El usuario E2E del back-office (`E2E_ADMIN_*` o `E2E_ADMIN_TOKEN`) en el grupo
  `analistas-siniestros`.
- Las coberturas sintéticas de HU-W21 ya aplicadas en `pagos-parametricos` (las deja la E2E de HU-W21
  con `E2E_PUBLICAR_COBERTURAS=1`). Con `E2E_PUBLICAR_COBERTURAS=1` esta prueba las publica también
  (**escribe en el tópico de eventos de INT**; mismo `eventId`, idempotente) y espera
  `E2E_COBERTURAS_ESPERA_S` (20 s) a que se apliquen. Sin cobertura el evento no activa ninguna póliza
  y C01 falla con ese motivo.
- C04 además: credenciales de AWS con lectura del bucket de auditoría y la salida `audit_bucket` de
  `infra/platform`; sin ellas se omite.

Cada ejecución usa un sufijo propio en el `idExterno` (`E2E_TELEMETRIA_SUFIJO` lo fija): sin él, repetir
la prueba sería un reenvío y no un evento nuevo, y no habría siniestro nuevo que ver. **Crea un siniestro
paramétrico de verdad**, que `dinero` paga por `adaptador-pagos` (el simulador). Espera al siniestro
`E2E_SINIESTRO_ESPERA_S` (por omisión 120 s: la cola de eventos, el relay del Outbox y la activación) y
a la auditoría `E2E_AUDITORIA_ESPERA_S` (180 s).
"""

import importlib.util
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

HISTORIA = "HU-W22 (R5)"
RAIZ = Path(__file__).parents[2]
DATOS = RAIZ / "contracts/datos-sinteticos/eventos-parametricos.json"
SCRIPT = RAIZ / "scripts/dev/firmar_evento.py"
ESPERA_SINIESTRO_S = int(os.getenv("E2E_SINIESTRO_ESPERA_S", "120"))
ESPERA_AUDITORIA_S = int(os.getenv("E2E_AUDITORIA_ESPERA_S", "180"))
ESPERA_COBERTURAS_S = int(os.getenv("E2E_COBERTURAS_ESPERA_S", "20"))
PAUSA_REPETIDOS_S = int(os.getenv("E2E_REPETIDOS_ESPERA_S", "10"))
TIMEOUT_S = 30
CLAVE_EVENTO = "valido"
CLAVE_POLIZA = "COB-BOG-1"
# Lo que C01 deja para C02, C03 y C04.
RESULTADO: dict[str, Any] = {}


class Portal:
    """El portal, por `/web`, con el usuario E2E del back-office (grupo `analistas-siniestros`)."""

    def __init__(self, base: str, token: str) -> None:
        self._base = f"{base}/web/v1/siniestros"
        self._cabeceras = {"Authorization": f"Bearer {token}"}

    def get(self, ruta: str, **params: str) -> httpx.Response:
        respuesta = httpx.get(
            f"{self._base}{ruta}", params=params, headers=self._cabeceras, timeout=TIMEOUT_S
        )
        assert respuesta.status_code != 403, (
            "el usuario E2E del back-office debe estar en el grupo analistas-siniestros "
            "(infra/DESPLIEGUE.md)"
        )
        return respuesta

    def resumen(self, evento_origen: str) -> dict[str, Any]:
        respuesta = self.get("/parametricos/resumen", eventoOrigen=evento_origen)
        assert respuesta.status_code == 200, respuesta.text
        cuerpo: dict[str, Any] = respuesta.json()
        return cuerpo

    def detalle(self, siniestro_id: str) -> dict[str, Any]:
        respuesta = self.get(f"/{siniestro_id}")
        assert respuesta.status_code == 200, respuesta.text
        cuerpo: dict[str, Any] = respuesta.json()
        return cuerpo

    def del_evento(self, evento_origen: str, evento_id: str) -> list[dict[str, Any]]:
        """Los detalles de los siniestros de `evento_id` entre los últimos pagos de H09."""
        filas = self.resumen(evento_origen)["ultimosPagos"]
        detalles = [self.detalle(fila["siniestroId"]) for fila in filas]
        return [d for d in detalles if (d.get("evento") or {}).get("eventoId") == evento_id]

    def esperar_siniestros(self, evento_origen: str, evento_id: str) -> list[dict[str, Any]]:
        limite = time.monotonic() + ESPERA_SINIESTRO_S
        while True:
            encontrados = self.del_evento(evento_origen, evento_id)
            if encontrados or time.monotonic() > limite:
                return encontrados
            time.sleep(5)


class Auditoria:
    """El tópico de eventos y los lotes `cadena/*.json` del bucket de auditoría (como HU-W21 C03)."""

    def __init__(self, salidas: dict[str, Any]) -> None:
        import boto3

        self.bucket: str = salidas["audit_bucket"]["name"]
        self.topico: str = salidas.get("events_topic_arn", "")
        self._s3 = boto3.client("s3")
        self._sns = boto3.client("sns")
        self.desde = datetime.now(UTC)

    def publicar(self, sobre: dict[str, Any]) -> None:
        self._sns.publish(
            TopicArn=self.topico,
            Message=json.dumps(sobre),
            MessageAttributes={"eventType": {"DataType": "String", "StringValue": sobre["type"]}},
        )

    def registros(self) -> list[dict[str, Any]]:
        """Los registros de los lotes escritos desde que empezó la prueba."""
        encontrados: list[dict[str, Any]] = []
        paginas = self._s3.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix="cadena/"
        )
        for pagina in paginas:
            for objeto in pagina.get("Contents", []):
                if objeto["LastModified"] < self.desde:
                    continue
                lote = json.loads(
                    self._s3.get_object(Bucket=self.bucket, Key=objeto["Key"])["Body"].read()
                )
                encontrados.extend(lote["registros"])
        return encontrados

    def esperar(self, condicion: Any, espera_s: int = ESPERA_AUDITORIA_S) -> list[dict[str, Any]]:
        limite = time.monotonic() + espera_s
        while True:
            registros = self.registros()
            if condicion(registros) or time.monotonic() > limite:
                return registros
            time.sleep(5)


@pytest.fixture(scope="module")
def firmador() -> ModuleType:
    """`scripts/dev/firmar_evento.py`: la fuente de telemetría sintética."""
    especificacion = importlib.util.spec_from_file_location("firmar_evento_e2e_w22", SCRIPT)
    assert especificacion and especificacion.loader
    modulo = importlib.util.module_from_spec(especificacion)
    sys.modules["firmar_evento_e2e_w22"] = modulo
    especificacion.loader.exec_module(modulo)
    return modulo


@pytest.fixture(scope="module")
def datos() -> dict[str, Any]:
    cargados: dict[str, Any] = json.loads(DATOS.read_text(encoding="utf-8"))
    return cargados


@pytest.fixture(scope="module")
def sufijo() -> str:
    return os.getenv("E2E_TELEMETRIA_SUFIJO") or uuid.uuid4().hex[:10]


@pytest.fixture(scope="module")
def config(ambiente: Any) -> dict[str, str]:
    base = os.getenv("E2E_TELEMETRIA_URL") or (
        f"{ambiente.urls['canales'].rstrip('/')}/telemetria/v1/eventos"
        if ambiente.urls["canales"]
        else ""
    )
    return ambiente.requerir(
        HISTORIA,
        {"url": base, "secreto": os.getenv("E2E_TELEMETRIA_SECRETO", "")},
    )


@pytest.fixture(scope="module")
def portal(ambiente: Any) -> Portal:
    config_portal = ambiente.requerir(
        HISTORIA, {"canales": ambiente.urls["canales"], "token": ambiente.token_admin()}
    )
    return Portal(config_portal["canales"], config_portal["token"])


def _auditoria(ambiente: Any) -> Auditoria:
    salidas = ambiente.salidas.get("platform", {})
    if not salidas.get("audit_bucket", {}).get("name"):
        pytest.skip("N3 HU-W22: falta la salida audit_bucket de infra/platform")
    try:
        return Auditoria(salidas)
    except Exception as error:  # noqa: BLE001 - sin credenciales de AWS no hay auditoría que leer
        pytest.skip(f"N3 HU-W22: sin acceso a AWS ({type(error).__name__})")


@pytest.fixture(scope="module")
def auditoria(ambiente: Any) -> Auditoria:
    return _auditoria(ambiente)


@pytest.fixture(scope="module")
def coberturas(ambiente: Any, datos: dict[str, Any]) -> None:
    """Con `E2E_PUBLICAR_COBERTURAS=1`, publica las coberturas sintéticas (idempotente: mismo
    `eventId`) y espera a que `pagos-parametricos` las aplique. Sin la variable no hace nada: se
    espera que HU-W21 las haya dejado."""
    if os.getenv("E2E_PUBLICAR_COBERTURAS") != "1":
        return
    publicador = _auditoria(ambiente)
    if not publicador.topico:
        pytest.skip("N3 HU-W22: falta la salida events_topic_arn de infra/platform")
    ids = {c["sobre"]["eventId"] for c in datos["coberturas"]}
    for entrada in datos["coberturas"]:
        publicador.publicar(entrada["sobre"])
    registros = publicador.esperar(
        lambda r: ids <= {x["eventId"] for x in r if x["accion"] == "PolizaEmitida"}
    )
    llegaron = {x["eventId"] for x in registros if x["accion"] == "PolizaEmitida"}
    assert ids <= llegaron, "las coberturas sintéticas no llegaron a auditoría"
    time.sleep(ESPERA_COBERTURAS_S)


def _caso(datos: dict[str, Any]) -> dict[str, Any]:
    return next(c for c in datos["eventos"] if c["clave"] == CLAVE_EVENTO)


def _cobertura(datos: dict[str, Any]) -> dict[str, Any]:
    entrada = next(c for c in datos["coberturas"] if c["clave"] == CLAVE_POLIZA)
    cobertura: dict[str, Any] = entrada["sobre"]["payload"]
    return cobertura


def _enviar(
    firmador: ModuleType, config: dict[str, str], caso: dict[str, Any], sufijo: str
) -> httpx.Response:
    envio = firmador.con_sufijo(caso, sufijo)
    encabezados, cuerpo = firmador.armar_envio(envio, datetime.now(UTC), config["secreto"])
    return httpx.post(
        config["url"],
        content=cuerpo,
        headers={**encabezados, "Content-Type": "application/json"},
        timeout=TIMEOUT_S,
    )


def test_hu_w22_r5_c01_el_evento_enviado_dos_veces_crea_un_solo_siniestro(
    config: dict[str, str],
    firmador: ModuleType,
    datos: dict[str, Any],
    sufijo: str,
    portal: Portal,
    coberturas: None,
) -> None:
    caso = _caso(datos)
    origen = caso["envio"]["cuerpo"]["eventoOrigen"]

    primero = _enviar(firmador, config, caso, sufijo)
    assert primero.status_code == 202, primero.text
    evento_id = primero.json()["eventoId"]
    reenvio = _enviar(firmador, config, caso, sufijo)
    assert reenvio.status_code == 200, reenvio.text
    assert reenvio.json() == primero.json()

    siniestros = portal.esperar_siniestros(origen, evento_id)
    assert siniestros, (
        f"el evento {evento_id} no creó ningún siniestro en {ESPERA_SINIESTRO_S} s: ¿las coberturas "
        "sintéticas de HU-W21 están aplicadas en pagos-parametricos? (E2E_PUBLICAR_COBERTURAS=1)"
    )
    # AC1: exactamente uno (la póliza activada es una; COB-BOG-2 queda fuera de ventana).
    assert len(siniestros) == 1, [s["codigo"] for s in siniestros]
    time.sleep(PAUSA_REPETIDOS_S)
    assert len(portal.del_evento(origen, evento_id)) == 1, (
        "una entrega repetida creó otro siniestro"
    )

    RESULTADO.update(
        eventoId=evento_id, origen=origen, siniestroId=siniestros[0]["id"], envio=caso["envio"]
    )


def test_hu_w22_r5_c02_el_detalle_nace_aprobado_con_el_evento_y_la_regla(
    portal: Portal, datos: dict[str, Any]
) -> None:
    assert RESULTADO, "C01 debe correr antes que C02"
    cobertura = _cobertura(datos)
    cuerpo = RESULTADO["envio"]["cuerpo"]
    detalle = portal.detalle(RESULTADO["siniestroId"])

    assert detalle["origen"] == "PARAMETRICO"
    assert detalle["estado"] == "APROBADO"
    assert detalle["analistaId"] is None
    assert detalle["evidencias"] == []
    assert detalle["polizaNumero"] == cobertura["numero"]
    assert detalle["causa"] == cuerpo["tipoEvento"]

    # AC3: el evento y la regla que lo activaron.
    evento = detalle["evento"]
    assert evento["eventoId"] == RESULTADO["eventoId"]
    assert evento["eventoOrigen"] == RESULTADO["origen"]
    assert evento["fuente"] == RESULTADO["envio"]["fuente"]
    assert evento["tipoEvento"] == cuerpo["tipoEvento"]
    # Los decimales viajan como texto y el servicio puede normalizarlos ("135" o "135.000").
    assert Decimal(evento["valorObservado"]) == Decimal(cuerpo["valorObservado"])
    assert evento["unidad"] == cuerpo["unidad"]
    assert Decimal(evento["valorObservado"]) >= Decimal(evento["umbral"])
    assert evento["regla"] == "VALOR_IGUAL_O_SUPERIOR_AL_UMBRAL"
    assert evento["versionRegla"]

    # Nace aprobado por el sistema, sin decisión de nadie.
    primero = detalle["historial"][0]
    assert (primero["accion"], primero["actorRol"], primero["actorId"]) == (
        "CREADO_AUTOMATICAMENTE",
        "SISTEMA_PARAMETRICO",
        None,
    )
    assert primero["estadoNuevo"] == "APROBADO"
    liquidacion = detalle["liquidacion"]
    assert liquidacion["valorReclamado"] is None
    assert liquidacion["deducible"] == "0.00"
    assert Decimal(liquidacion["valorIndemnizar"]) == Decimal(cobertura["sumaAsegurada"])
    assert liquidacion["moneda"] == cobertura["moneda"]
    assert detalle["pago"]["estado"] in ("PENDIENTE", "CONFIRMADO")
    assert detalle["pago"]["referencia"] == liquidacion["referenciaPago"]
    # Nunca la cuenta completa: solo la enmascarada.
    assert detalle["poliza"]["beneficiario"]["cuentaMascarada"].startswith("****")

    # No hay decisión que tomar: el caso no está en «Mi bandeja» ni en la cola sin asignar.
    for bandeja in ("MIA", "SIN_ASIGNAR"):
        cola = portal.get("", bandeja=bandeja, origen="PARAMETRICO", limite="50")
        assert cola.status_code == 200, cola.text
        assert RESULTADO["siniestroId"] not in {i["id"] for i in cola.json()["items"]}, bandeja


def test_hu_w22_r5_c03_h09_cuenta_el_evento_y_lista_el_pago_activado(portal: Portal) -> None:
    assert RESULTADO, "C01 debe correr antes que C03"
    resumen = portal.resumen(RESULTADO["origen"])
    assert resumen["eventoOrigen"] == RESULTADO["origen"]
    assert resumen["recibidos"] >= 1
    assert resumen["procesados"] >= 1
    assert resumen["activados"] >= 1
    assert resumen["duplicados"] >= 0
    pagos = {p["siniestroId"]: p for p in resumen["ultimosPagos"]}
    fila = pagos[RESULTADO["siniestroId"]]
    assert fila["estadoActivacion"] == "SINIESTRO_CREADO"
    assert fila["disparador"]["tipoEvento"] == RESULTADO["envio"]["cuerpo"]["tipoEvento"]
    # `null` es una fuente que no respondió (CloudWatch o siniestros): nunca un cero inventado.
    assert fila["estadoPago"] in (None, "PENDIENTE", "CONFIRMADO")
    # El resumen no trae lo que no debe: ni la cuenta, ni el cuerpo del evento, ni la firma.
    assert "cuenta" not in json.dumps(resumen).lower() and "firma" not in json.dumps(resumen)


def test_hu_w22_r5_c04_auditoria_recibe_un_solo_siniestro_aprobado(auditoria: Auditoria) -> None:
    assert RESULTADO, "C01 debe correr antes que C04"
    siniestro_id = RESULTADO["siniestroId"]
    registros = auditoria.esperar(
        lambda r: any(
            x["accion"] == "SiniestroAprobado" and x["resourceId"] == siniestro_id for x in r
        )
    )
    aprobados = [
        x
        for x in registros
        if x["accion"] == "SiniestroAprobado" and x["resourceId"] == siniestro_id
    ]
    # AC1 y SEC-W22-01: el siniestro nace aprobado una sola vez, aunque el evento se haya reenviado.
    assert len(aprobados) == 1, f"{len(aprobados)} SiniestroAprobado de {siniestro_id} en auditoría"
    sobre = aprobados[0]["evento"]
    sobre = json.loads(sobre) if isinstance(sobre, str) else sobre
    texto = json.dumps(sobre)
    # Sin el valor observado, la fuente ni el beneficiario en el evento (SEC-W22-01).
    assert "valorObservado" not in texto and "beneficiarioRef" not in texto
    assert RESULTADO["envio"]["fuente"] not in texto
