"""HU-W25 (paso 25.5, §b-14): el apagado de pagos-parametricos cabe en el `stopTimeout` de ECS.

Tras SIGTERM, uvicorn 0.54.0 (uv.lock) hace en serie, en `Server.shutdown` (uvicorn/server.py):
cierra los sockets y espera 0,1 s (`:293`), espera a las solicitudes HTTP en curso hasta
`--timeout-graceful-shutdown` (`:296-307`) y **después** cierra el lifespan (`:309-311`). El
lifespan (LIFO, main.py `crear_app`) para los dos consumidores a la vez (`PARADA_CONSUMIDOR_S` para
el conjunto), luego el relay (`PARADA_RELAY_S`) y luego el pool (`AsyncConnectionPool.close`, con su
`timeout` por omisión). ECS manda SIGKILL `stopTimeout` segundos después del SIGTERM
(infra/apps/ecs.tf:165). La suma tiene que ser menor: por eso el tope HTTP es de 4 s y no de 25
(ajuste de la 25.5). Cada término se lee de donde sale; si no se encuentra, la prueba falla. Forma
de services/cotizacion/tests/module/test_hu_w07_dockerfile.py.
"""

import inspect
import json
import re
from pathlib import Path

import pytest
import uvicorn.server
from pagos_parametricos import main
from psycopg_pool import AsyncConnectionPool

RAIZ = Path(__file__).resolve().parents[4]
DOCKERFILE = RAIZ / "services" / "pagos-parametricos" / "Dockerfile"
ECS_TF = RAIZ / "infra" / "apps" / "ecs.tf"
FLAG = "--timeout-graceful-shutdown"
TOPE_HTTP_S = 4
# infra/apps/ecs.tf:165 (`stopTimeout = 30` en la definición de contenedor de todos los servicios).
STOP_TIMEOUT_S = 30


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


def tope_http_del_cmd() -> int:
    cmd = cmd_del_dockerfile()
    assert FLAG in cmd, f"el CMD no tiene {FLAG}: {cmd}"
    assert cmd.count(FLAG) == 1, cmd
    indice = cmd.index(FLAG)
    assert indice + 1 < len(cmd), f"{FLAG} sin valor: {cmd}"
    valor = cmd[indice + 1]
    assert valor.isdigit(), f"{FLAG} debe ser un entero de segundos: {valor!r}"
    return int(valor)


def stop_timeout() -> int:
    assert ECS_TF.is_file(), f"no se encontró {ECS_TF}"
    valores = re.findall(r"\bstopTimeout\s*=\s*(\d+)\b", ECS_TF.read_text(encoding="utf-8"))
    assert len(valores) == 1, f"se esperaba un solo stopTimeout en {ECS_TF}: {valores}"
    return int(valores[0])


def cierre_de_sockets_s() -> float:
    """La pausa tras cerrar los sockets, del código de uvicorn (`Server.shutdown`, server.py:293)."""
    fuente = inspect.getsource(uvicorn.server.Server.shutdown)
    pausas = re.findall(r"await asyncio\.sleep\(([0-9.]+)\)", fuente)
    assert len(pausas) == 1, f"se esperaba una sola pausa en Server.shutdown: {pausas}"
    return float(pausas[0])


def tope_cierre_pool_s() -> float:
    """`crear_app` registra `pila.push_async_callback(pool_base.close)` (main.py, `ciclo`), sin
    argumentos: vale el `timeout` por omisión de `AsyncConnectionPool.close`
    (psycopg_pool/pool_async.py:497)."""
    assert "pila.push_async_callback(pool_base.close)" in inspect.getsource(main.crear_app)
    tope = inspect.signature(AsyncConnectionPool.close).parameters["timeout"].default
    assert isinstance(tope, int | float), tope
    return float(tope)


def tope_relay_s() -> float:
    """`_relay` entra a `_en_fondo` sin `limite_s` (main.py:295): vale su valor por omisión, que es
    `PARADA_RELAY_S` (main.py:55)."""
    tope = inspect.signature(main._en_fondo).parameters["limite_s"].default
    assert tope == main.PARADA_RELAY_S, (tope, main.PARADA_RELAY_S)
    return float(tope)


def test_el_cmd_tiene_un_tope_http_de_4_s() -> None:
    assert tope_http_del_cmd() == TOPE_HTTP_S, f"{FLAG}: el ajuste de la 25.5 fija {TOPE_HTTP_S}"


def test_el_stop_timeout_documentado_es_el_de_ecs_tf() -> None:
    assert stop_timeout() == STOP_TIMEOUT_S, "cambió stopTimeout en ecs.tf: revisa el presupuesto"


def test_el_apagado_completo_cabe_en_el_stop_timeout_de_ecs() -> None:
    """Los términos en el orden en que corren; ninguno se solapa con otro."""
    terminos = {
        "cierre de sockets (uvicorn/server.py:293)": cierre_de_sockets_s(),
        f"solicitudes HTTP ({FLAG} del CMD, Dockerfile)": float(tope_http_del_cmd()),
        "los dos consumidores a la vez (main.PARADA_CONSUMIDOR_S)": float(main.PARADA_CONSUMIDOR_S),
        "el relay (main.PARADA_RELAY_S)": tope_relay_s(),
        "el pool (AsyncConnectionPool.close, timeout)": tope_cierre_pool_s(),
    }
    total = sum(terminos.values())
    detalle = " + ".join(f"{v:g} [{k}]" for k, v in terminos.items())
    print(f"\nHU-W25 presupuesto del apagado: {detalle} = {total:g} s < {STOP_TIMEOUT_S} s")
    assert total < STOP_TIMEOUT_S, f"{detalle} = {total:g} s; stopTimeout = {STOP_TIMEOUT_S}"


def test_el_cmd_sigue_sirviendo_la_app_de_pagos_parametricos() -> None:
    """La prueba de SIGTERM reemplaza la app y el puerto: tienen que estar donde los espera."""
    cmd = cmd_del_dockerfile()
    assert cmd[:2] == ["uvicorn", "pagos_parametricos.main:app"]
    assert cmd[cmd.index("--host") + 1] == "0.0.0.0"
    assert cmd[cmd.index("--port") + 1] == "8080"
