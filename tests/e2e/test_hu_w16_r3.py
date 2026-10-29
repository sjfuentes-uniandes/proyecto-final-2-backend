"""N3 R3 (HU-W16) contra un ambiente desplegado: de la aceptación al recaudo de la prima.

Recorre lo mismo que la versión de módulo (`services/dinero/tests/module/test_r3_recaudo_w16.py`)
con lo que se ve desde fuera: el portal para aceptar, el tópico de eventos para los casos sin
suscripción aprobada y los logs de CloudWatch (`/ecs/<prefijo>/<servicio>`) de `dinero`,
`adaptador-pagos` y `polizas`, por `clave_recaudo` y `correlation_id` (solo lectura). Sin acceso a
las bases ni a las colas: leer una cola le quitaría los mensajes a su consumidor.

- C01, aprobado: portal (`/web`, usuario E2E del back-office en `asesores`) → aceptación de S01
  → dinero registra el cobro resuelto `CONFIRMADO`, con la correlación de la aceptación, y
  adaptador-pagos midió un solo `cobro.pagos`.
- C02 y C06: solo S01 tiene una suscripción aprobada (C01), así que su `OfertaAceptada`
  sintético se publica en el tópico con `eventType`, como lo haría el relay de polizas (en
  módulo se inserta en su outbox). C02 → `RECHAZADO`; C06 → incierto `TIEMPO_AGOTADO` y la
  conciliación lo confirma.
- Duplicado: el mismo sobre publicado dos veces seguidas (si hay varias tareas de dinero,
  compiten por él): un solo `cobro.pagos`.
- Inspección: en los logs leídos de polizas y dinero, ningún dato de tarjeta ni de cuenta.

C07 (proveedor caído) solo se verifica a nivel de módulo
(`test_c07_caido_sigue_pendiente_tras_conciliar_con_reintento_y_sin_evento`). En `int` quedaría
`PENDIENTE` para siempre: la conciliación lo vuelve a consultar en cada ciclo, sin tope, y pasada
una hora lo mide como `error`, lo que hace sonar la alarma de tasa de error de dinero.

Requiere EXT-01 (`polizas.calls` += `cotizacion`, `catalogo`) y EXT-02 en `int`, como la R3 de
HU-W13 a HU-W15, y credenciales de AWS que puedan publicar en el tópico de eventos y leer los
logs. La conciliación espera la antigüedad mínima (5 min) y su intervalo (2 min):
`E2E_CONCILIACION_ESPERA_S`, por defecto 600. Sin configuración se omite como las demás N3.

Configuración y precondiciones en infra/DESPLIEGUE.md (sección 7, "Pruebas E2E (N3)").
"""

import json
import os
import re
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

HISTORIA = "HU-W16 (R3)"
TIMEOUT_S = 30
ESPERA_RECAUDO_S = int(os.getenv("E2E_RECAUDO_ESPERA_S", "120"))
ESPERA_CONCILIACION_S = int(os.getenv("E2E_CONCILIACION_ESPERA_S", "600"))
# Tras resolver el duplicado, cuánto se espera a que aparezca un segundo cobro (no debe).
ESPERA_DUPLICADO_S = 60
RAIZ = Path(__file__).parents[2]
S01 = next(
    c
    for c in json.loads(
        (RAIZ / "contracts/datos-sinteticos/cotizaciones-suscripcion.json").read_text(
            encoding="utf-8"
        )
    )["casos"]
    if c["id"] == "S01"
)
VERSION_DECLARACIONES = "2026.10.1"
DECLARACIONES = {
    "leidoCondiciones": True,
    "informacionVeraz": True,
    "debitoAutomatico": True,
    "avisosElectronicos": True,
}
# Pagadores sintéticos (= clienteId) de simulador-aliados/mappings/pagos/. C01 es el de S01.
C01, C02, C06 = (f"00000000-0000-4000-8000-00000000000{n}" for n in (1, 2, 6))
# Los mensajes de dinero (application/recaudo/recaudar.py y conciliar.py).
RESUELTO = "recaudo: cobro resuelto"
INCIERTO = "recaudo: cobro incierto; queda PENDIENTE"
CONCILIADO = "conciliacion: movimiento conciliado"
# Inspección (ADR 0013, decisión 6), con las reglas de la versión de módulo.
CLAVES_PROHIBIDAS = re.compile(
    r"tarjeta|card|cvv|cvc|cuenta|account|iban|swift|bancari|banco|bank|expir|vencimiento|"
    r"titular|holder"
)
EXACTOS = {"pan", "pin", "nip", "numero", "number", "cbu", "clabe", "routing"}
NUMERO_DE_TARJETA = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
UUID = re.compile(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", re.IGNORECASE)
MEDIDOS = ("duration_ms", "trace_id", "span_id")


class Portal:
    """El portal, por `/web`, con el usuario E2E del back-office (grupo `asesores`)."""

    def __init__(self, base: str, token: str) -> None:
        self._base = f"{base}/web/v1"
        self._cabeceras = {"Authorization": f"Bearer {token}"}

    def post(self, ruta: str, cuerpo: dict[str, Any]) -> httpx.Response:
        respuesta = httpx.post(
            f"{self._base}{ruta}",
            json=cuerpo,
            headers={**self._cabeceras, "Idempotency-Key": str(uuid.uuid4())},
            timeout=TIMEOUT_S,
        )
        assert respuesta.status_code != 403 or "IDENTIDAD" in respuesta.text, (
            "el usuario E2E del back-office debe estar en el grupo asesores (infra/DESPLIEGUE.md)"
        )
        return respuesta

    def aceptar_s01(self) -> httpx.Response:
        """Perfil, cotización, decisión APROBADA y aceptación de S01 (C01, 36 años)."""
        datos = {
            "fechaNacimiento": "1990-04-18",
            "plazoCreditoMeses": 180,
            "sumaAsegurada": 180000000,
        }
        perfil = self.post(
            "/perfiles",
            {"clienteId": C01, "productoId": "vida-hipotecario", "datosDeclarados": datos},
        )
        assert perfil.status_code in (200, 201), perfil.text
        cotizacion = self.post(
            "/cotizaciones",
            {
                "clienteId": C01,
                "productoId": "vida-hipotecario",
                "datosAsegurado": datos,
                "perfilId": perfil.json()["id"],
            },
        )
        assert cotizacion.status_code == 201, cotizacion.text
        decision = self.post("/suscripciones", {"cotizacionId": cotizacion.json()["id"]})
        assert decision.status_code == 201, decision.text
        assert decision.json()["resultado"] == "APROBADA", decision.text
        aceptacion = self.post(
            f"/cotizaciones/{decision.json()['cotizacionId']}/aceptacion",
            {
                "decisionId": decision.json()["id"],
                "versionDeclaraciones": VERSION_DECLARACIONES,
                "declaraciones": DECLARACIONES,
            },
        )
        assert aceptacion.status_code == 201, aceptacion.text
        return aceptacion


class Aws:
    """El tópico de eventos (publicar) y los logs de los servicios (leer)."""

    def __init__(self, region: str, prefijo: str, topico: str) -> None:
        import boto3

        self._sns = boto3.client("sns", region_name=region)
        self._logs = boto3.client("logs", region_name=region)
        self._prefijo = prefijo
        self._topico = topico

    def publicar(self, sobre: dict[str, Any]) -> None:
        self._sns.publish(
            TopicArn=self._topico,
            Message=json.dumps(sobre),
            MessageAttributes={"eventType": {"DataType": "String", "StringValue": sobre["type"]}},
        )

    def registros(
        self, servicio: str, campo: str, valor: str, desde: datetime
    ) -> list[dict[str, Any]]:
        """Las líneas JSON del servicio con `campo` = `valor` desde `desde`."""
        pedido: dict[str, Any] = {
            "logGroupName": f"/ecs/{self._prefijo}/{servicio}",
            "filterPattern": f'{{ $.{campo} = "{valor}" }}',
            "startTime": int(desde.timestamp() * 1000),
        }
        lineas: list[dict[str, Any]] = []
        while True:
            respuesta = self._logs.filter_log_events(**pedido)
            lineas += [json.loads(e["message"]) for e in respuesta["events"]]
            if not respuesta.get("nextToken"):
                return lineas
            pedido["nextToken"] = respuesta["nextToken"]

    def esperar(
        self,
        servicio: str,
        campo: str,
        valor: str,
        desde: datetime,
        condicion: Callable[[dict[str, Any]], bool],
        espera_s: int,
    ) -> list[dict[str, Any]]:
        """Los registros que cumplen `condicion`, en cuanto haya alguno (o vacío al vencer)."""
        limite = time.monotonic() + espera_s
        while True:
            encontrados = [r for r in self.registros(servicio, campo, valor, desde) if condicion(r)]
            if encontrados or time.monotonic() > limite:
                return encontrados
            time.sleep(10)

    def cobros(self, correlacion: str, desde: datetime) -> list[str]:
        """Los resultados medidos de `cobro.pagos` en adaptador-pagos para esa correlación."""
        return [
            r["result"]
            for r in self.registros("adaptador-pagos", "correlation_id", correlacion, desde)
            if r.get("operation") == "cobro.pagos" and r.get("result")
        ]


def oferta_aceptada(pagador: str) -> dict[str, Any]:
    """El `OfertaAceptada` v1 que publicaría el relay de polizas, con la prima de S01."""
    ahora = datetime.now(UTC)
    clave = str(uuid.uuid4())
    insumos = S01["insumos"]
    return {
        "eventId": str(uuid.uuid4()),
        "aggregateId": clave,
        "type": "OfertaAceptada",
        "version": 1,
        "occurredAt": ahora.isoformat(),
        "correlationId": str(uuid.uuid4()),
        "payload": {
            "aceptacionId": clave,
            "cotizacionId": str(uuid.uuid4()),
            "ofertaId": None,
            "decisionId": str(uuid.uuid4()),
            "clienteId": pagador,
            "productoId": insumos["productoId"],
            "prima": {
                "valor": insumos["primaTotal"],
                "moneda": insumos["moneda"],
                "periodicidad": insumos["periodicidad"],
            },
            "claveRecaudo": clave,
            "huellaCalculo": insumos["huellaCalculo"],
            "vigenteHasta": (ahora + timedelta(days=30)).isoformat(),
            "versionDeclaraciones": VERSION_DECLARACIONES,
            "aceptadaEn": ahora.isoformat(),
            "actorId": "asesor-sintetico-e2e",
            "canal": "PORTAL_ASESOR",
        },
    }


def _claves(valor: Any) -> list[str]:
    if isinstance(valor, dict):
        return [c for k, v in valor.items() for c in (str(k), *_claves(v))]
    if isinstance(valor, list):
        return [c for v in valor for c in _claves(v)]
    return []


def sin_datos_de_pago(registros: list[dict[str, Any]]) -> None:
    assert registros, "la inspección no tendría nada que revisar"
    for registro in registros:
        linea = {k: v for k, v in registro.items() if k not in MEDIDOS}
        claves = [re.sub(r"[^a-z0-9]", "", c.lower()) for c in _claves(linea)]
        assert [c for c in claves if c in EXACTOS or CLAVES_PROHIBIDAS.search(c)] == []
        assert not NUMERO_DE_TARJETA.search(UUID.sub("", json.dumps(linea, ensure_ascii=False)))


def es(mensaje: str, **campos: Any) -> Callable[[dict[str, Any]], bool]:
    return lambda r: r.get("message") == mensaje and all(r.get(k) == v for k, v in campos.items())


@pytest.fixture(scope="module")
def portal(ambiente: Any) -> Portal:
    config = ambiente.requerir(
        HISTORIA, {"canales": ambiente.urls["canales"], "admin_token": ambiente.token_admin()}
    )
    return Portal(config["canales"], config["admin_token"])


@pytest.fixture(scope="module")
def aws(ambiente: Any) -> Aws:
    plataforma = ambiente.salidas.get("platform", {})
    config = ambiente.requerir(
        HISTORIA,
        {
            "region": os.getenv("AWS_REGION", plataforma.get("region", "")),
            "prefijo": os.getenv("E2E_PREFIJO", plataforma.get("prefix", "")),
            "topico": os.getenv("E2E_EVENTS_TOPIC_ARN", plataforma.get("events_topic_arn", "")),
        },
    )
    try:
        return Aws(config["region"], config["prefijo"], config["topico"])
    except ImportError:
        pytest.skip(f"N3 {HISTORIA}: boto3 no está instalado")


def _hace_un_minuto() -> datetime:
    # Margen para el reloj de CloudWatch.
    return datetime.now(UTC) - timedelta(minutes=1)


def test_hu_w16_r3_c01_la_aceptacion_se_recauda_y_confirma_una_sola_vez(
    portal: Portal, aws: Aws
) -> None:
    desde = _hace_un_minuto()
    aceptacion = portal.aceptar_s01()
    clave = aceptacion.json()["id"]
    correlacion = aceptacion.headers["X-Correlation-Id"]

    resueltos = aws.esperar("dinero", "clave_recaudo", clave, desde, es(RESUELTO), ESPERA_RECAUDO_S)
    assert [(r["estado"], r["correlation_id"]) for r in resueltos] == [("CONFIRMADO", correlacion)]
    assert aws.cobros(correlacion, desde) == ["ok"]
    print(f"\nHU-W16 R3: aceptación {clave} recaudada (correlación {correlacion})")
    sin_datos_de_pago(
        aws.registros("dinero", "clave_recaudo", clave, desde)
        + aws.registros("polizas", "correlation_id", correlacion, desde)
    )


def test_hu_w16_r3_c02_el_rechazo_del_proveedor_rechaza_el_recaudo(aws: Aws) -> None:
    desde = _hace_un_minuto()
    sobre = oferta_aceptada(C02)
    clave = sobre["payload"]["claveRecaudo"]
    aws.publicar(sobre)

    resueltos = aws.esperar("dinero", "clave_recaudo", clave, desde, es(RESUELTO), ESPERA_RECAUDO_S)
    assert [(r["estado"], r["motivo"]) for r in resueltos] == [
        ("RECHAZADO", "FONDOS_INSUFICIENTES")
    ]
    assert aws.cobros(sobre["correlationId"], desde) == ["rechazado"]
    sin_datos_de_pago(aws.registros("dinero", "clave_recaudo", clave, desde))


def test_hu_w16_r3_c06_el_lento_queda_pendiente_y_la_conciliacion_lo_confirma(aws: Aws) -> None:
    desde = _hace_un_minuto()
    sobre = oferta_aceptada(C06)
    clave = sobre["payload"]["claveRecaudo"]
    aws.publicar(sobre)

    inciertos = aws.esperar("dinero", "clave_recaudo", clave, desde, es(INCIERTO), ESPERA_RECAUDO_S)
    assert [(r["estado"], r["codigo"]) for r in inciertos] == [("PENDIENTE", "TIEMPO_AGOTADO")]
    conciliados = aws.esperar(
        "dinero", "clave_recaudo", clave, desde, es(CONCILIADO), ESPERA_CONCILIACION_S
    )
    assert [(r["resultado"], r["reintento"]) for r in conciliados[:1]] == [("confirmado", False)]
    registros = aws.registros("dinero", "clave_recaudo", clave, desde)
    assert not [r for r in registros if r.get("message") == RESUELTO]  # lo resolvió la conciliación
    sin_datos_de_pago(registros)


def test_hu_w16_r3_el_mismo_oferta_aceptada_publicado_dos_veces_cobra_una_sola_vez(
    aws: Aws,
) -> None:
    desde = _hace_un_minuto()
    sobre = oferta_aceptada(C01)
    clave = sobre["payload"]["claveRecaudo"]
    aws.publicar(sobre)
    aws.publicar(sobre)

    resueltos = aws.esperar("dinero", "clave_recaudo", clave, desde, es(RESUELTO), ESPERA_RECAUDO_S)
    assert [r["estado"] for r in resueltos] == ["CONFIRMADO"]
    time.sleep(ESPERA_DUPLICADO_S)
    registros = aws.registros("dinero", "clave_recaudo", clave, desde)
    assert len([r for r in registros if r.get("message") == RESUELTO]) == 1
    assert aws.cobros(sobre["correlationId"], desde) == ["ok"]
    sin_datos_de_pago(registros)
