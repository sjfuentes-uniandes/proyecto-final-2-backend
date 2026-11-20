"""El cableado de HU-W22 en `main.py`: el consumidor de `parametricos-eventos` en modo (b) más
`reanudar`, conservando la forma de W24, y la ACL hacia `siniestros`."""

import asyncio
from typing import Any

import httpx
import pytest
from pagos_parametricos import main
from pagos_parametricos.adapters.outbound.descartes_pg import RegistroFallosPg
from pagos_parametricos.adapters.outbound.siniestros_http import SiniestrosHttp
from pagos_parametricos.config import Settings
from pagos_parametricos.main import armar_consumidores, armar_siniestros
from solventa_common.inbox import cliente_sqs

EVENTOS = "http://localstack:4566/000000000000/solventa-local-parametricos-eventos"
POLIZAS = "http://localstack:4566/000000000000/solventa-local-parametricos-polizas"


class PoolInerte:
    """Un pool que nadie usa: armar los consumidores no abre conexiones."""


class SiniestrosInerte:
    async def crear(self, solicitud: Any) -> Any:
        raise AssertionError("no se llama al armar")


@pytest.fixture(autouse=True)
def credenciales(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("SQS_PARAMETRICOS_EVENTOS_URL", EVENTOS)
    monkeypatch.setenv("SQS_PARAMETRICOS_POLIZAS_URL", POLIZAS)


def _cliente() -> Any:
    return cliente_sqs("us-east-1", endpoint_url="http://localhost:4566")


def test_con_siniestros_el_consumidor_de_eventos_tiene_reanudar_y_conserva_la_forma_de_w24() -> (
    None
):
    consumidores = armar_consumidores(
        PoolInerte(),  # type: ignore[arg-type]
        _cliente(),
        main.RelojSistema(),
        Settings(),
        SiniestrosInerte(),
    )

    eventos, polizas = consumidores["consumidor_eventos"], consumidores["consumidor_polizas"]
    assert eventos._reanudar is not None and polizas._reanudar is None
    # La forma de W24 (§9): concurrencia, visibilidad de 30 s, plazo de parada y causa del fallo.
    assert (eventos._concurrencia, eventos._visibilidad_s, eventos._plazo_parada_s) == (10, 30, 10)
    assert getattr(eventos._al_fallar, "__func__", None) is RegistroFallosPg.al_fallar
    assert eventos._receptor.max_mensajes == 10  # type: ignore[attr-defined]


def test_sin_siniestros_no_hay_reanudar_y_es_el_modo_a_de_w21() -> None:
    consumidores = armar_consumidores(
        PoolInerte(),  # type: ignore[arg-type]
        _cliente(),
        main.RelojSistema(),
        Settings(),
    )

    assert consumidores["consumidor_eventos"]._reanudar is None


def test_la_acl_hacia_siniestros_usa_la_configuracion_de_ds3_23() -> None:
    ajustes = Settings(SINIESTROS_URL="http://siniestros:8080")

    async def verificar() -> None:
        async with httpx.AsyncClient() as http:
            acl = armar_siniestros(http, ajustes)
            assert isinstance(acl, SiniestrosHttp)
            assert acl._timeout_ms == 600 and acl._base_url == "http://siniestros:8080"
            proteccion = acl._proteccion
            assert (proteccion._bulkhead_max, proteccion._fallos_consecutivos) == (10, 5)  # type: ignore[attr-defined]

    asyncio.run(verificar())


@pytest.mark.parametrize("valor", ["0", "701", "-5"])
def test_el_timeout_hacia_siniestros_no_supera_el_corte_duro_de_700_ms(
    monkeypatch: pytest.MonkeyPatch, valor: str
) -> None:
    monkeypatch.setenv("SINIESTROS_TIMEOUT_MS", valor)

    with pytest.raises(ValueError):
        Settings()


def test_por_omision_el_timeout_es_600_y_el_bulkhead_10(monkeypatch: pytest.MonkeyPatch) -> None:
    ajustes = Settings()

    assert (ajustes.siniestros_timeout_ms, ajustes.siniestros_bulkhead_max) == (600, 10)
