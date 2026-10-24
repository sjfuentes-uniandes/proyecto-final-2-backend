"""N3 R1 (HU-W05) contra un ambiente desplegado: una fuente lenta no bloquea la cotización.

Por el borde real del portal (`/web`, usuario E2E del back-office en el grupo `asesores`):

1. Perfil del cliente sintético C06, cuya fuente `DA_REGISTROS_PUBLICOS` está configurada `lento`
   en el simulador (1000 ms, contracts/datos-sinteticos): `cotizacion` la abandona en su timeout
   (`SOLVENTA_FUENTE_TIMEOUT_MS`, 600 en `int`), el perfil sale DEGRADADO con la fuente en
   TIEMPO_AGOTADO y la respuesta llega a tiempo (AC1).
2. Cotización con ese perfil: se calcula dentro del presupuesto del recorrido
   (`latency_budget_ms.cotizacion`, 700 en `int`) (AC3).
3. La traza en X-Ray (por `annotation.correlation_id`) muestra la dependencia agotada con
   `solventa.timeout_ms` y su resultado `timeout`.

Sin carga. La traza exige credenciales de AWS con xray:GetTraceSummaries y BatchGetTraces
(`AWS_PROFILE` o variables); sin ellas, esa prueba se omite con el motivo.
Configuración y precondiciones en infra/DESPLIEGUE.md (sección 7, "Pruebas E2E (N3)").
"""

import json
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

HISTORIA = "HU-W05 (R1)"
TIMEOUT_S = 30
CLIENTE_C06 = os.getenv("E2E_W05_CLIENTE_ID", "00000000-0000-4000-8000-000000000006")
FUENTE_LENTA = "DA_REGISTROS_PUBLICOS"
# Datos declarados de C06 en contracts/datos-sinteticos/clientes-perfilamiento.json.
DATOS = {"fechaNacimiento": "1990-04-18", "plazoCreditoMeses": 180, "sumaAsegurada": 180000000}
PRESUPUESTO_COTIZACION_MS = int(os.getenv("E2E_W05_PRESUPUESTO_MS", "700"))
FUENTE_TIMEOUT_MS = int(os.getenv("E2E_W05_FUENTE_TIMEOUT_MS", "600"))
# Saltos por el borde (API Gateway, VPC Link, bff-web): no son parte del recorrido medido.
MARGEN_BORDE_MS = int(os.getenv("E2E_W05_MARGEN_BORDE_MS", "600"))
ESPERA_TRAZA_S = int(os.getenv("E2E_W05_ESPERA_TRAZA_S", "90"))


class Portal:
    def __init__(self, base: str, token: str) -> None:
        self._base = f"{base}/web/v1"
        self._cabeceras = {"Authorization": f"Bearer {token}"}

    def post(
        self, ruta: str, cuerpo: dict[str, Any], correlacion: str
    ) -> tuple[httpx.Response, float]:
        inicio = time.perf_counter()
        respuesta = httpx.post(
            f"{self._base}{ruta}",
            json=cuerpo,
            headers={
                **self._cabeceras,
                "Idempotency-Key": str(uuid.uuid4()),
                "X-Correlation-Id": correlacion,
            },
            timeout=TIMEOUT_S,
        )
        assert respuesta.status_code != 403, "el usuario E2E debe estar en el grupo asesores"
        return respuesta, (time.perf_counter() - inicio) * 1000


@pytest.fixture(scope="module")
def portal(ambiente: Any) -> Portal:
    config = ambiente.requerir(
        HISTORIA, {"canales": ambiente.urls["canales"], "admin_token": ambiente.token_admin()}
    )
    return Portal(config["canales"], config["admin_token"])


@pytest.fixture(scope="module")
def recorrido(portal: Portal) -> dict[str, Any]:
    """Perfil de C06 con la fuente lenta y la cotización con ese perfil (una sola vez)."""
    correlacion = f"e2e-hu-w05-{uuid.uuid4().hex[:12]}"
    perfil, perfil_ms = portal.post(
        "/perfiles",
        {"clienteId": CLIENTE_C06, "productoId": "vida-hipotecario", "datosDeclarados": DATOS},
        correlacion,
    )
    assert perfil.status_code == 201, perfil.text
    cotizacion, cotizacion_ms = portal.post(
        "/cotizaciones",
        {
            "clienteId": CLIENTE_C06,
            "productoId": "vida-hipotecario",
            "perfilId": perfil.json()["id"],
            "datosAsegurado": DATOS,
        },
        correlacion,
    )
    print(
        f"\nHU-W05 R1 {correlacion}: perfil {perfil_ms:.0f} ms, cotización {cotizacion_ms:.0f} ms"
    )
    return {
        "correlacion": correlacion,
        "perfil": perfil,
        "perfil_ms": perfil_ms,
        "cotizacion": cotizacion,
        "cotizacion_ms": cotizacion_ms,
    }


def test_hu_w05_r1_la_fuente_lenta_se_abandona_y_el_perfil_llega_a_tiempo(
    recorrido: dict[str, Any],
) -> None:
    perfil = recorrido["perfil"].json()
    assert perfil["estado"] == "DEGRADADO"
    fuentes = {f["fuente"]: f["estado"] for f in perfil["fuentes"]}
    assert fuentes[FUENTE_LENTA] == "TIEMPO_AGOTADO"
    # El aliado tarda 1000 ms: la respuesta llega antes porque la rama se abandonó.
    assert recorrido["perfil_ms"] < FUENTE_TIMEOUT_MS + MARGEN_BORDE_MS, recorrido["perfil_ms"]


def test_hu_w05_r1_la_cotizacion_llega_dentro_del_presupuesto(recorrido: dict[str, Any]) -> None:
    respuesta = recorrido["cotizacion"]
    assert respuesta.status_code == 201, respuesta.text
    cotizacion = respuesta.json()
    assert cotizacion["oferta"] is not None
    assert cotizacion["duracionMs"] <= PRESUPUESTO_COTIZACION_MS, cotizacion["duracionMs"]
    assert recorrido["cotizacion_ms"] < PRESUPUESTO_COTIZACION_MS + MARGEN_BORDE_MS


def _xray() -> Any:
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:  # pragma: no cover
        pytest.skip(f"N3 {HISTORIA}: boto3 no está instalado")
    try:
        cliente = boto3.client("xray", region_name=os.getenv("AWS_REGION", "us-east-1"))
        cliente.get_sampling_rules()  # comprueba credenciales y permisos
    except (BotoCoreError, ClientError) as error:
        motivo = f"N3 {HISTORIA}: sin acceso a X-Ray ({type(error).__name__})"
        if os.getenv("E2E_REQUERIDO") == "1":
            pytest.fail(motivo)
        pytest.skip(motivo)
    return cliente


def _documentos(xray: Any, correlacion: str) -> list[dict[str, Any]]:
    """Segmentos de las trazas con la correlación, esperando a que X-Ray las indexe."""
    filtro = f'annotation.correlation_id = "{correlacion}"'
    limite = time.monotonic() + ESPERA_TRAZA_S
    while True:
        ahora = datetime.now(UTC)
        resumenes = xray.get_trace_summaries(
            StartTime=ahora - timedelta(minutes=15), EndTime=ahora, FilterExpression=filtro
        )["TraceSummaries"]
        ids = [r["Id"] for r in resumenes]
        if ids or time.monotonic() > limite:
            break
        time.sleep(5)
    assert ids, f"X-Ray no indexó la traza de {correlacion} en {ESPERA_TRAZA_S} s"
    documentos: list[dict[str, Any]] = []
    for inicio in range(0, len(ids), 5):
        for traza in xray.batch_get_traces(TraceIds=ids[inicio : inicio + 5])["Traces"]:
            documentos.extend(json.loads(s["Document"]) for s in traza["Segments"])
    return documentos


def _subsegmentos(documento: dict[str, Any]) -> list[dict[str, Any]]:
    pendientes, todos = [documento], []
    while pendientes:
        actual = pendientes.pop()
        todos.append(actual)
        pendientes.extend(actual.get("subsegments", []))
    return todos


def _atributos(segmento: dict[str, Any]) -> dict[str, Any]:
    # ADOT indexa correlation_id y operation como anotaciones; el resto va a metadata.default.
    return {
        **segmento.get("annotations", {}),
        **segmento.get("metadata", {}).get("default", {}),
    }


def test_hu_w05_r1_la_traza_muestra_la_dependencia_agotada(recorrido: dict[str, Any]) -> None:
    xray = _xray()
    segmentos = [s for d in _documentos(xray, recorrido["correlacion"]) for s in _subsegmentos(d)]
    rama = f"perfilamiento.dependencia.fuente.{FUENTE_LENTA}"
    agotadas = [s for s in segmentos if s.get("name") in (rama, f"consulta.{FUENTE_LENTA}")]
    assert agotadas, f"sin el segmento {rama} en la traza"
    atributos = [_atributos(s) for s in agotadas]
    assert any(a.get("result") == "timeout" for a in atributos), atributos
    # adaptador-datos anota el timeout efectivo y el X-Deadline-Ms recibido de cotizacion.
    consulta = [a for a in atributos if "solventa.timeout_ms" in a]
    assert consulta, atributos
    assert all(a["solventa.timeout_ms"] <= FUENTE_TIMEOUT_MS for a in consulta)
    # La cotización del mismo recorrido lleva su presupuesto.
    cotizaciones = [_atributos(s) for s in segmentos if s.get("name") == "cotizacion"]
    assert any(
        a.get("solventa.presupuesto_ms") == PRESUPUESTO_COTIZACION_MS for a in cotizaciones
    ), cotizaciones
