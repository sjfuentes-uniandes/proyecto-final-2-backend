"""N2/N3 HU-W02 contra un ambiente desplegado con el plan "prueba" (1 solicitud/s, ráfaga 2, 5 al día).

Necesita credenciales de AWS para reponer la cuota de los socios de prueba y leer logs y métricas.
"""

import json
import os
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

import httpx
import pytest

CUOTA = 5
PAUSA_S = 1.2  # por encima de 1 solicitud/s: el límite por segundo no interfiere con la cuota
RETRY_AFTER_LIMITE = os.getenv("E2E_RETRY_AFTER_LIMITE", "1")
RETRY_AFTER_CUOTA = os.getenv("E2E_RETRY_AFTER_CUOTA", "3600")
ESPERA_PLAN_S = int(os.getenv("E2E_ESPERA_PLAN_S", "60"))  # API Gateway aplica el plan nuevo de forma eventual
ESPERA_LOGS_S = int(os.getenv("E2E_ESPERA_LOGS_S", "240"))
T = TypeVar("T")


class Aws:
    def __init__(self, salidas: dict[str, Any]) -> None:
        import boto3

        plataforma = salidas.get("platform", {})
        region = os.getenv("AWS_REGION") or plataforma.get("region") or "us-east-1"
        self.prefijo = plataforma.get("prefix") or f"solventa-{os.getenv('ENV', 'int')}"
        self.plan_prueba = plataforma.get("usage_plans", {}).get("prueba", "")
        self.logs_borde = plataforma.get("apis", {}).get("socios", {}).get("log_group", "")
        self.apigateway = boto3.client("apigateway", region_name=region)
        self.logs = boto3.client("logs", region_name=region)
        self.cloudwatch = boto3.client("cloudwatch", region_name=region)

    def id_de_api_key(self, valor: str) -> str:
        paginas = self.apigateway.get_paginator("get_api_keys").paginate(includeValues=True)
        return next(k["id"] for pagina in paginas for k in pagina["items"] if k.get("value") == valor)

    def reponer_cuota(self, api_key_id: str) -> None:
        self.apigateway.update_usage(usagePlanId=self.plan_prueba, keyId=api_key_id,
                                     patchOperations=[{"op": "replace", "path": "/remaining", "value": str(CUOTA)}])

    def eventos(self, grupo: str, patron: str, desde: datetime) -> list[dict[str, Any]]:
        respuesta = self.logs.filter_log_events(logGroupName=grupo, filterPattern=patron,
                                                startTime=int(desde.timestamp() * 1000))
        return respuesta.get("events", [])

    def limitadas(self, api_key_id: str, desde: datetime) -> int:
        datos = self.cloudwatch.get_metric_statistics(
            Namespace="Solventa/Socios", MetricName="PartnerThrottled",
            Dimensions=[{"Name": "ApiKeyId", "Value": api_key_id}],
            StartTime=desde - timedelta(minutes=1), EndTime=datetime.now(UTC) + timedelta(minutes=1),
            Period=60, Statistics=["Sum"])
        return int(sum(p["Sum"] for p in datos.get("Datapoints", [])))


def _esperar(condicion: Callable[[], T], espera_s: int, pausa_s: float = 10) -> T:
    limite = time.monotonic() + espera_s
    while True:
        valor = condicion()
        if valor or time.monotonic() > limite:
            return valor
        time.sleep(pausa_s)


def _acceso(ambiente, config: dict[str, str], client_id: str, secreto: str, api_key: str) -> dict[str, str]:
    token = ambiente.hasta(lambda: ambiente.token(config["token_url"], client_id, secreto), 200)
    assert token.status_code == 200, token.text
    return {"Authorization": f"Bearer {token.json()['access_token']}", "x-api-key": api_key}


def _agotar(url: str, acceso: dict[str, str]) -> tuple[list[int], httpx.Response]:
    """Hasta CUOTA solicitudes procesadas y la siguiente; un 429 THROTTLED se reintenta."""
    procesadas: list[int] = []
    while True:
        respuesta = httpx.get(url, headers=acceso, timeout=30)
        if respuesta.status_code == 429 and respuesta.json().get("error") == "THROTTLED":
            time.sleep(float(respuesta.headers.get("retry-after", "1")) + 0.2)
            continue
        if respuesta.status_code != 200 or len(procesadas) == CUOTA:
            return procesadas, respuesta
        procesadas.append(respuesta.status_code)
        time.sleep(PAUSA_S)


@pytest.fixture
def aws(ambiente) -> Iterator[Aws]:
    try:
        cliente = Aws(ambiente.salidas)
        cliente.apigateway.get_account()
    except Exception as error:  # noqa: BLE001 - sin credenciales la prueba se omite o falla según E2E_REQUERIDO
        if os.getenv("E2E_REQUERIDO") == "1":
            pytest.fail(f"N2/N3 HU-W02 sin credenciales de AWS: {error}")
        pytest.skip(f"N2/N3 HU-W02 sin credenciales de AWS: {error}")
    yield cliente


@pytest.fixture
def socios_de_prueba(ambiente, aws: Aws) -> Iterator[dict[str, dict[str, str]]]:
    socios = {nombre: ambiente.socio_de_prueba(nombre) for nombre in ("socio-a", "socio-b")}
    ambiente.requerir("HU-W02", {
        **ambiente.urls, "plan_prueba": aws.plan_prueba, "logs_borde": aws.logs_borde,
        **{f"{n}_{c}": v for n, datos in socios.items() for c, v in datos.items() if c != "api_key_id"},
    })
    for datos in socios.values():
        datos["api_key_id"] = datos["api_key_id"] or aws.id_de_api_key(datos["api_key"])
        aws.reponer_cuota(datos["api_key_id"])
    yield socios
    for datos in socios.values():
        aws.reponer_cuota(datos["api_key_id"])


def test_hu_w02_n2_cuota_por_credencial_en_el_borde(ambiente, aws: Aws, socios_de_prueba) -> None:
    config = ambiente.requerir("HU-W02", ambiente.urls)
    a, b = socios_de_prueba["socio-a"], socios_de_prueba["socio-b"]
    acceso_a = _acceso(ambiente, config, a["client_id"], a["client_secret"], a["api_key"])
    acceso_b = _acceso(ambiente, config, b["client_id"], b["client_secret"], b["api_key"])
    inicio = datetime.now(UTC)

    # Límite por segundo: la ráfaga de 2 se supera y el 429 pide reintentar en 1 s.
    with ThreadPoolExecutor(max_workers=6) as hilos:
        rafaga = list(hilos.map(lambda _: httpx.get(f"{config['socios']}/health", headers=acceso_a, timeout=30),
                                range(6)))
    limitadas = [r for r in rafaga if r.status_code == 429]
    assert limitadas, [r.status_code for r in rafaga]
    assert {r.json()["error"] for r in limitadas} == {"THROTTLED"}
    assert {r.headers["retry-after"] for r in limitadas} == {RETRY_AFTER_LIMITE}
    aws.reponer_cuota(a["api_key_id"])
    time.sleep(2)

    # AC1 y AC2: cinco solicitudes procesadas y la sexta rechazada en el borde.
    procesadas, sexta = _agotar(f"{config['socios']}/health", acceso_a)
    assert procesadas == [200] * CUOTA, procesadas
    assert sexta.status_code == 429, sexta.text
    assert sexta.json()["error"] == "QUOTA_EXCEEDED"
    assert sexta.headers["retry-after"] == RETRY_AFTER_CUOTA
    solicitud = sexta.headers["x-correlation-id"]
    assert solicitud and sexta.json()["correlationId"] == solicitud

    # AC3: B conserva su capacidad.
    assert httpx.get(f"{config['socios']}/health", headers=acceso_b, timeout=30).status_code == 200

    # AC2: la solicitud rechazada no llegó a api-socios.
    acceso = _esperar(lambda: aws.eventos(aws.logs_borde, f'{{ $.requestId = "{solicitud}" }}', inicio), ESPERA_LOGS_S)
    assert acceso, f"Sin registro de acceso para {solicitud}"
    registro = json.loads(acceso[0]["message"])
    assert (registro["status"], registro["integrationStatus"]) == ("429", "-")
    assert registro["apiKeyId"] == a["api_key_id"]
    assert not aws.eventos(f"/ecs/{aws.prefijo}/api-socios", f'"{solicitud}"', inicio)

    # AC2: la limitación queda en las métricas de A y no en las de B.
    assert _esperar(lambda: aws.limitadas(a["api_key_id"], inicio), ESPERA_LOGS_S) >= 1
    assert aws.limitadas(b["api_key_id"], inicio) == 0


def test_hu_w02_n3_cambio_de_plan_desde_la_administracion(ambiente, aws: Aws, socios_de_prueba) -> None:
    config = ambiente.requerir("HU-W02", {"admin_token": ambiente.token_admin(), **ambiente.urls})
    admin = {"Authorization": f"Bearer {config['admin_token']}"}
    web = f"{config['canales']}/web/socios"
    b = socios_de_prueba["socio-b"]

    planes = httpx.get(f"{web}/planes", headers=admin, timeout=30)
    assert planes.status_code == 200, planes.text
    assert {p["plan"]: p["cuota"] for p in planes.json()}.get("prueba") == CUOTA

    alta = httpx.post(web, headers=admin, timeout=30, json={
        "nombre": f"E2E HU-W02 {time.strftime('%Y%m%d%H%M%S')}", "canal": "comercio", "plan": "basico",
        "permisos": ["cotizar"],
    })
    assert alta.status_code == 201, alta.text
    socio = alta.json()
    try:
        cambio = httpx.put(f"{web}/{socio['id']}/plan", headers=admin, json={"plan": "prueba"}, timeout=30)
        assert cambio.status_code == 200 and cambio.json()["plan"] == "prueba", cambio.text
        consumo = httpx.get(f"{web}/{socio['id']}/consumo", headers=admin, timeout=30).json()
        assert consumo["limites"]["cuota"] == CUOTA and consumo["limites"]["plan"] == "prueba"
        assert "apiKey" not in str(consumo) and "clientSecret" not in str(consumo)

        acceso = _acceso(ambiente, config, socio["clientId"], socio["clientSecret"], socio["apiKey"])
        assert ambiente.hasta(lambda: httpx.get(f"{config['socios']}/socio/me", headers=acceso, timeout=30),
                              200).status_code == 200
        time.sleep(ESPERA_PLAN_S)
        api_key_id = aws.id_de_api_key(socio["apiKey"])
        aws.reponer_cuota(api_key_id)
        inicio = datetime.now(UTC)

        # Operación de negocio atribuida al socio (AC1) hasta agotar la cuota nueva (AC2).
        procesadas, sexta = _agotar(f"{config['socios']}/socio/me", acceso)
        assert procesadas == [200] * CUOTA, procesadas
        assert sexta.status_code == 429 and sexta.json()["error"] == "QUOTA_EXCEEDED", sexta.text
        assert sexta.headers["retry-after"] == RETRY_AFTER_CUOTA

        acceso_b = _acceso(ambiente, config, b["client_id"], b["client_secret"], b["api_key"])
        assert httpx.get(f"{config['socios']}/health", headers=acceso_b, timeout=30).status_code == 200

        def agotada() -> dict[str, Any] | None:
            actual = httpx.get(f"{web}/{socio['id']}/consumo", headers=admin, timeout=30).json()
            return actual if actual["estado"] == "agotada" and (actual["limitadas"] or 0) >= 1 else None

        final = _esperar(agotada, ESPERA_LOGS_S)
        assert final, "El consumo no reflejó la cuota agotada ni la limitación"
        assert (final["restante"], final["porcentaje"]) == (0, 100)
        assert aws.limitadas(api_key_id, inicio) >= 1
    finally:
        desactivado = httpx.post(f"{web}/{socio['id']}/desactivar", headers=admin, timeout=30)
        assert desactivado.status_code == 200
