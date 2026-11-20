"""HU-W07 (paso 7.6): el CMD de cotizacion drena antes de que ECS mate la tarea.

uvicorn espera `--timeout-graceful-shutdown` segundos a las solicitudes en curso tras SIGTERM; ECS
manda SIGKILL `stopTimeout` segundos después del SIGTERM (infra/apps/ecs.tf). El primero tiene que
ser menor. Los dos valores se leen de sus archivos: si no se encuentran, la prueba falla.
"""

import json
import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[4]
DOCKERFILE = RAIZ / "services" / "cotizacion" / "Dockerfile"
ECS_TF = RAIZ / "infra" / "apps" / "ecs.tf"
FLAG = "--timeout-graceful-shutdown"


def cmd_del_dockerfile() -> list[str]:
    assert DOCKERFILE.is_file(), f"no se encontró {DOCKERFILE}"
    lineas = [
        linea.strip()
        for linea in DOCKERFILE.read_text(encoding="utf-8").splitlines()
        if linea.strip().startswith("CMD")
    ]
    assert len(lineas) == 1, f"se esperaba un solo CMD en {DOCKERFILE}: {lineas}"
    try:
        cmd = json.loads(lineas[0].removeprefix("CMD").strip())
    except json.JSONDecodeError as error:
        pytest.fail(f"el CMD de {DOCKERFILE} no está en forma JSON: {error}")
    assert isinstance(cmd, list) and all(isinstance(a, str) for a in cmd), cmd
    return cmd


def stop_timeout() -> int:
    assert ECS_TF.is_file(), f"no se encontró {ECS_TF}"
    valores = re.findall(r"\bstopTimeout\s*=\s*(\d+)\b", ECS_TF.read_text(encoding="utf-8"))
    assert len(valores) == 1, f"se esperaba un solo stopTimeout en {ECS_TF}: {valores}"
    return int(valores[0])


def test_el_cmd_drena_menos_tiempo_que_el_stop_timeout_de_ecs() -> None:
    cmd = cmd_del_dockerfile()
    assert FLAG in cmd, f"el CMD no tiene {FLAG}: {cmd}"
    indice = cmd.index(FLAG)
    assert indice + 1 < len(cmd), f"{FLAG} sin valor: {cmd}"
    valor = cmd[indice + 1]
    assert valor.isdigit(), f"{FLAG} debe ser un entero de segundos: {valor!r}"
    limite = stop_timeout()
    assert 0 < int(valor) < limite, f"{FLAG} = {valor} debe ser menor que stopTimeout = {limite}"


def test_el_cmd_sigue_sirviendo_la_app_de_cotizacion() -> None:
    """La prueba de drenaje reemplaza la app y el puerto: tienen que estar donde los espera."""
    cmd = cmd_del_dockerfile()
    assert cmd[:2] == ["uvicorn", "cotizacion.main:app"]
    assert cmd[cmd.index("--port") + 1] == "8080"
