"""HU-W28: PartnerQuotaUsage por socio para las alarmas de cuota."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from io import StringIO
from typing import Any

import pytest
from api_socios import main
from api_socios.adapters.inbound import cuotas_periodicas, dependencias
from api_socios.adapters.outbound.cloudwatch import MetricasCloudWatch
from api_socios.application.socios import AdministrarSocios
from api_socios.config import settings
from api_socios.domain.socios import CanalVenta, NuevoSocio, Permiso, Plan
from fastapi.testclient import TestClient

AHORA = datetime(2026, 10, 13, 15, 30, tzinfo=UTC)


class Grabadora:
    def __init__(self) -> None:
        self.llamadas: list[tuple[str, dict[str, Any]]] = []

    def put_metric_data(self, **kwargs: Any) -> dict[str, Any]:
        self.llamadas.append(("put_metric_data", kwargs))
        return {}


class MetricasFalsas:
    def __init__(self) -> None:
        self.consultas: list[str] = []

    def limitadas(self, api_key_id: str, desde: datetime, hasta: datetime) -> int:
        self.consultas.append(api_key_id)
        return 0


def nuevo(nombre: str, plan: Plan = Plan.PRUEBA) -> NuevoSocio:
    return NuevoSocio(nombre, CanalVenta.COMERCIO, plan, frozenset({Permiso.COTIZAR}))


def test_porcentaje_de_cuota_de_cada_socio_activo(entorno) -> None:
    metricas = MetricasFalsas()
    caso = AdministrarSocios(entorno.repositorio, entorno.identidad, entorno.api_keys, metricas)
    a, _ = caso.habilitar(nuevo("Retail Andino"))
    b, _ = caso.habilitar(nuevo("Seguros Caribe", Plan.BASICO))
    c, _ = caso.habilitar(nuevo("Inactivo"))
    caso.desactivar(c.id)
    entorno.api_keys.usado.update({a.api_key_id: 5, b.api_key_id: 900, c.api_key_id: 5})

    assert caso.porcentajes_de_cuota(AHORA) == {a.id: 100, b.id: 90}
    assert metricas.consultas == []  # sin GetMetricStatistics por socio


def test_un_socio_sin_uso_legible_no_detiene_a_los_demas(
    entorno, capturar_logs: Callable[[], StringIO], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _ = entorno.caso.habilitar(nuevo("Retail Andino"))
    b, _ = entorno.caso.habilitar(nuevo("Seguros Caribe"))
    entorno.api_keys.usado[b.api_key_id] = 1
    uso = entorno.api_keys.uso

    def uso_con_falla(api_key_id, *args):  # type: ignore[no-untyped-def]
        if api_key_id == a.api_key_id:
            raise ConnectionError("apigateway no disponible")
        return uso(api_key_id, *args)

    monkeypatch.setattr(entorno.api_keys, "uso", uso_con_falla)
    salida = capturar_logs()
    assert entorno.caso.porcentajes_de_cuota(AHORA) == {b.id: 20}
    assert "no se pudo medir la cuota del socio" in salida.getvalue()
    assert a.id in salida.getvalue() and "ConnectionError" in salida.getvalue()


def test_publica_partner_quota_usage_por_socio() -> None:
    cloudwatch = Grabadora()
    MetricasCloudWatch(cloudwatch, "int").publicar_cuotas({"SOC-000002": 90, "SOC-000001": 100})
    operacion, kwargs = cloudwatch.llamadas[0]
    assert operacion == "put_metric_data" and kwargs["Namespace"] == "Solventa/Socios"
    assert kwargs["MetricData"] == [
        {
            "MetricName": "PartnerQuotaUsage",
            "Dimensions": [
                {"Name": "Environment", "Value": "int"},
                {"Name": "Partner", "Value": socio},
            ],
            "Value": valor,
            "Unit": "Percent",
        }
        for socio, valor in [("SOC-000001", 100.0), ("SOC-000002", 90.0)]
    ]


def test_publica_en_lotes_y_sin_socios_no_llama() -> None:
    cloudwatch = Grabadora()
    metricas = MetricasCloudWatch(cloudwatch, "int")
    metricas.publicar_cuotas({})
    assert cloudwatch.llamadas == []
    metricas.publicar_cuotas({f"SOC-{i:06d}": 1 for i in range(1001)})
    assert [len(kwargs["MetricData"]) for _, kwargs in cloudwatch.llamadas] == [1000, 1]


def test_publicar_cuotas_mide_y_publica(entorno, monkeypatch: pytest.MonkeyPatch) -> None:
    socio, _ = entorno.caso.habilitar(nuevo("Retail Andino"))
    entorno.api_keys.usado[socio.api_key_id] = 2
    cloudwatch = Grabadora()
    monkeypatch.setattr(dependencias, "_administrar", lambda: entorno.caso)
    monkeypatch.setattr(dependencias, "metricas", lambda: MetricasCloudWatch(cloudwatch, "int"))
    assert dependencias.publicar_cuotas() == 1
    dimensiones = cloudwatch.llamadas[0][1]["MetricData"][0]["Dimensions"]
    assert {"Name": "Partner", "Value": socio.id} in dimensiones


def test_el_ciclo_reintenta_despues_de_una_falla(capturar_logs: Callable[[], StringIO]) -> None:
    llamadas: list[int] = []

    def publicar() -> int:
        llamadas.append(1)
        if len(llamadas) == 1:
            raise ConnectionError("cloudwatch no disponible")
        return 3

    async def correr() -> None:
        tarea = asyncio.create_task(cuotas_periodicas.publicar_periodicamente(publicar, 0.01))
        while len(llamadas) < 2:
            await asyncio.sleep(0.005)
        tarea.cancel()

    salida = capturar_logs()
    asyncio.run(correr())
    assert "no se pudo publicar la cuota de socios" in salida.getvalue()
    assert "cuota de socios publicada" in salida.getvalue()


@pytest.mark.parametrize(
    ("intervalo", "pool", "inicia"),
    [(300, "us-east-1_p", True), (0, "us-east-1_p", False), (300, None, False)],
)
def test_el_arranque_publica_solo_si_esta_configurado(
    monkeypatch: pytest.MonkeyPatch, intervalo: int, pool: str | None, inicia: bool
) -> None:
    iniciadas: list[float] = []

    async def falsa(publicar, intervalo_s):  # type: ignore[no-untyped-def]
        iniciadas.append(intervalo_s)
        await asyncio.Event().wait()

    monkeypatch.setattr(cuotas_periodicas, "publicar_periodicamente", falsa)
    monkeypatch.setattr(settings, "db_host", None)
    monkeypatch.setattr(settings, "database_url_override", None)
    monkeypatch.setattr(settings, "quota_metric_interval_s", intervalo)
    monkeypatch.setattr(settings, "partners_user_pool_id", pool)
    monkeypatch.setattr(settings, "partners_usage_plans", {"prueba": "p3"})
    with TestClient(main.app) as cliente:
        assert cliente.get("/health").status_code == 200
    assert iniciadas == ([300] if inicia else [])
