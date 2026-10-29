"""Arnés de módulo: WireMock 3.9.2 real con los mapeos versionados del simulador.

Necesita Docker; sin él, las pruebas fallan (no se omiten). Se corre desde la raíz del repo con:
uv run pytest services/simulador-aliados/tests/module
"""

import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from testcontainers.core.container import DockerContainer

SIMULADOR = Path(__file__).resolve().parents[2]
IMAGEN = "wiremock/wiremock:3.9.2"


def _esperar_listo(url: str, segundos: float = 60) -> None:
    limite = time.monotonic() + segundos
    while time.monotonic() < limite:
        try:
            if httpx.get(f"{url}/__admin/health", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError("WireMock no arrancó")


@pytest.fixture(scope="session")
def simulador() -> Iterator[httpx.Client]:
    """Cliente HTTP contra el simulador, con las opciones por defecto del Dockerfile."""
    contenedor = (
        DockerContainer(IMAGEN)
        .with_exposed_ports(8080)
        .with_volume_mapping(str(SIMULADOR / "mappings"), "/home/wiremock/mappings", "ro")
        .with_volume_mapping(str(SIMULADOR / "__files"), "/home/wiremock/__files", "ro")
        .with_command("--disable-banner")
    )
    with contenedor:
        url = f"http://{contenedor.get_container_host_ip()}:{contenedor.get_exposed_port(8080)}"
        _esperar_listo(url)
        with httpx.Client(base_url=url, timeout=20) as cliente:
            yield cliente
