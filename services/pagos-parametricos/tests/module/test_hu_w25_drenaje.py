"""HU-W25 (paso 25.5, AC3 y la regla de `stopTimeout`): una tarea que se retira drena sus colas.

uvicorn corre en un subproceso con exactamente los argumentos del CMD del Dockerfile, leídos del
archivo, salvo la app (apoyo_drenaje.py: la app real con dobles sin base ni red) y el puerto (uno
libre). Recibe SIGTERM, como lo manda ECS al retirar la tarea. Técnica de
services/cotizacion/tests/module/test_hu_w07_drenaje.py.

- A: cada consumidor tiene un mensaje en curso (unos 3 s) y una espera larga abierta. Tras el SIGTERM
  llegan mensajes a esas esperas: se liberan sin empezar (los dos consumidores ya tienen la parada).
  Los que estaban en curso terminan, se confirman y se borran. El proceso sale a tiempo.
- B: un manejador más largo (60 s) que el plazo de la parada (`PLAZO_PARADA_S`): se cancela, el
  inbox hace rollback, el mensaje se libera y el proceso sale tras el plazo y a tiempo.

«A tiempo» es antes del tope HTTP del CMD más `PARADA_CONSUMIDOR_S`: aquí no hay solicitudes HTTP
en curso, ni relay, ni pool real. Que el apagado completo (con relay y pool) cabe en el
`stopTimeout` de ECS lo prueba tests/unit/test_hu_w25_dockerfile.py.

En Windows se omite: SIGTERM no es una parada ordenada (CLAUDE.md). La CI corre en Linux.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from pagos_parametricos.main import (
    MENSAJE_CONSUMO_SIN_PARAR,
    PARADA_CONSUMIDOR_S,
    PLAZO_PARADA_S,
)
from solventa_common.inbox import MENSAJE_LIBERADO

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="SIGTERM no es una parada ordenada en Windows"
)

RAIZ = Path(__file__).resolve().parents[4]
DOCKERFILE = RAIZ / "services" / "pagos-parametricos" / "Dockerfile"
APOYO = Path(__file__).resolve().parent
FLAG = "--timeout-graceful-shutdown"
APP = "pagos_parametricos.main:app"
# uvicorn 0.54.0 (uv.lock): uvicorn/lifespan/on.py y uvicorn/server.py.
ESPERA_APAGADO = "Waiting for application shutdown."
APAGADO_COMPLETO = "Application shutdown complete."
TOPE_VENCIDO = "running task(s), timeout graceful shutdown exceeded"
FIN_DEL_SERVIDOR = "Finished server process"
# Tras un apagado ordenado por señal, uvicorn restaura el manejador original y vuelve a lanzar la
# señal (uvicorn/server.py, capture_signals): el proceso termina por SIGTERM (143 en el contenedor),
# no con 0. Que el apagado se completó lo prueban los mensajes de arriba.
SALIDA_TRAS_SIGTERM = -signal.SIGTERM

Accion = tuple[str, str, str]


def cmd_del_dockerfile() -> list[str]:
    assert DOCKERFILE.is_file(), f"no se encontró {DOCKERFILE}"
    lineas = [
        linea.strip()
        for linea in DOCKERFILE.read_text(encoding="utf-8").splitlines()
        if linea.strip().startswith("CMD")
    ]
    assert len(lineas) == 1, f"se esperaba un solo CMD en {DOCKERFILE}: {lineas}"
    cmd: list[str] = json.loads(lineas[0].removeprefix("CMD").strip())
    return cmd


def valor_del_dockerfile() -> str:
    cmd = cmd_del_dockerfile()
    if FLAG not in cmd:
        pytest.fail(f"el CMD del Dockerfile no tiene {FLAG}: no hay valor que probar ({cmd})")
    return cmd[cmd.index(FLAG) + 1]


def limite_de_salida() -> float:
    """El tope HTTP del CMD (no hay solicitudes en curso) más el límite de los consumidores; el
    doble no tiene relay (`OUTBOX_ENABLED=False`) ni pool que cerrar (apoyo_drenaje.py)."""
    return float(valor_del_dockerfile()) + PARADA_CONSUMIDOR_S


def puerto_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        puerto: int = s.getsockname()[1]
    return puerto


def argumentos(puerto: int) -> list[str]:
    """Los del CMD, con otra app y otro puerto; el tope es el del Dockerfile."""
    cmd = cmd_del_dockerfile()
    valor_del_dockerfile()
    argumentos = list(cmd[1:])
    argumentos[argumentos.index(APP)] = "apoyo_drenaje:app"
    argumentos[argumentos.index("--port") + 1] = str(puerto)
    return [sys.executable, "-m", cmd[0], *argumentos]


@dataclass
class Servidor:
    proceso: subprocess.Popen[str]
    url: str
    lineas: list[str] = field(default_factory=list)

    def salida(self) -> str:
        return "\n".join(self.lineas)

    def acciones(self) -> set[Accion]:
        """Las líneas `DOBLE <acción> <cola> <id>` del doble de SQS, del inbox y de los manejadores."""
        acciones: set[Accion] = set()
        for linea in list(self.lineas):
            partes = linea.split()
            if len(partes) == 4 and partes[0] == "DOBLE":
                acciones.add((partes[1], partes[2], partes[3]))
        return acciones

    def colas(self) -> dict[str, tuple[str, list[str]]]:
        """Por cola, su mensaje inicial y sus tardíos, como los anuncia el doble al arrancar."""
        colas: dict[str, tuple[str, list[str]]] = {}
        for accion, cola, valor in self.acciones():
            if accion == "cola":
                inicial, tardios = (v.split("=", 1)[1] for v in valor.split(";"))
                colas[cola] = (inicial, tardios.split(","))
        return colas


def esperar(condicion: Callable[[], bool], tope_s: float, que: str, s: Servidor) -> None:
    limite = time.monotonic() + tope_s
    while not condicion():
        if time.monotonic() > limite:
            pytest.fail(f"no se cumplió en {tope_s} s: {que}\n{s.salida()}")
        time.sleep(0.05)


@contextmanager
def servidor(demora_s: float, llegada: Path | None = None) -> Iterator[Servidor]:
    puerto = puerto_libre()
    entorno = {**os.environ, "PYTHONPATH": str(APOYO), "DRENAJE_DEMORA_S": str(demora_s)}
    entorno.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
    entorno.pop("DRENAJE_LLEGADA", None)
    if llegada is not None:
        entorno["DRENAJE_LLEGADA"] = str(llegada)
    proceso = subprocess.Popen(
        argumentos(puerto),
        cwd=RAIZ,
        env=entorno,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    s = Servidor(proceso, f"http://127.0.0.1:{puerto}")
    assert proceso.stdout is not None
    lector = threading.Thread(
        target=lambda: s.lineas.extend(linea.rstrip("\n") for linea in proceso.stdout),  # type: ignore[union-attr]
        daemon=True,
    )
    lector.start()
    try:
        limite = time.monotonic() + 30
        while True:
            if proceso.poll() is not None:
                lector.join(5)
                pytest.fail(f"uvicorn terminó al arrancar ({proceso.returncode}):\n{s.salida()}")
            try:
                if httpx.get(f"{s.url}/health", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > limite:
                pytest.fail(f"uvicorn no respondió /health en 30 s:\n{s.salida()}")
            time.sleep(0.1)
        yield s
    finally:
        if proceso.poll() is None:
            proceso.kill()
            proceso.wait(10)
        lector.join(5)


def esperar_salida(s: Servidor, tope_s: float) -> int:
    try:
        return s.proceso.wait(timeout=tope_s)
    except subprocess.TimeoutExpired:
        pytest.fail(f"uvicorn no salió en {tope_s} s:\n{s.salida()}")


def esperar_en_curso(s: Servidor) -> dict[str, tuple[str, list[str]]]:
    esperar(lambda: len(s.colas()) == 2, 10, "el doble anuncia sus dos colas", s)
    colas = s.colas()
    en_curso = {("empieza", cola, inicial) for cola, (inicial, _) in colas.items()}
    esperar(lambda: en_curso <= s.acciones(), 10, "un mensaje en curso por consumidor", s)
    time.sleep(0.3)  # cada consumidor vuelve a su espera larga
    return colas


def test_a_lo_en_curso_termina_lo_recibido_se_libera_y_sale_a_tiempo(
    tmp_path: Path,
) -> None:
    limite = limite_de_salida()
    llegada = tmp_path / "llegan-los-tardios"
    with servidor(demora_s=3, llegada=llegada) as s:
        colas = esperar_en_curso(s)
        senal = time.monotonic()
        s.proceso.send_signal(signal.SIGTERM)
        esperar(lambda: ESPERA_APAGADO in s.salida(), 5, "uvicorn cierra el lifespan", s)
        time.sleep(0.5)  # la parada ya está dada cuando llegan los tardíos
        llegada.touch()
        codigo = esperar_salida(s, limite)
        duracion = time.monotonic() - senal
    salida, acciones = s.salida(), s.acciones()

    print(f"\nHU-W25 drenaje A: salió con {codigo} a los {duracion:.1f} s del SIGTERM\n{salida}")
    for cola, (inicial, tardios) in colas.items():
        assert {
            ("termina", cola, inicial),
            ("commit", "-", inicial),
            ("borrado", cola, inicial),
        } <= acciones, (cola, inicial)
        assert ("liberado", cola, inicial) not in acciones, (cola, inicial)
        for tardio in tardios:
            # La espera larga abandonada lo recibió con la parada ya dada: se libera sin empezar.
            assert ("empieza", cola, tardio) not in acciones, f"{cola} recibió tras la parada"
            assert {("recibido", cola, tardio), ("liberado", cola, tardio)} <= acciones, tardio
            assert ("borrado", cola, tardio) not in acciones, tardio
    assert codigo == SALIDA_TRAS_SIGTERM and duracion < limite, (duracion, limite)
    for mensaje in (ESPERA_APAGADO, MENSAJE_LIBERADO, APAGADO_COMPLETO, FIN_DEL_SERVIDOR):
        assert mensaje in salida, (mensaje, salida)
    assert TOPE_VENCIDO not in salida and MENSAJE_CONSUMO_SIN_PARAR not in salida


def test_b_un_manejador_mas_largo_que_el_plazo_se_cancela_se_libera_y_sale_a_tiempo() -> None:
    limite = limite_de_salida()
    with servidor(demora_s=60) as s:
        colas = esperar_en_curso(s)
        senal = time.monotonic()
        s.proceso.send_signal(signal.SIGTERM)
        codigo = esperar_salida(s, limite)
        duracion = time.monotonic() - senal
    salida, acciones = s.salida(), s.acciones()

    print(f"\nHU-W25 drenaje B: salió con {codigo} a los {duracion:.1f} s del SIGTERM\n{salida}")
    for cola, (inicial, _) in colas.items():
        assert {("rollback", "-", inicial), ("liberado", cola, inicial)} <= acciones, inicial
        for accion in ("termina", "borrado"):
            assert (accion, cola, inicial) not in acciones, (accion, cola, inicial)
        assert ("commit", "-", inicial) not in acciones, inicial
    assert codigo == SALIDA_TRAS_SIGTERM
    assert PLAZO_PARADA_S - 0.5 <= duracion < limite, (duracion, limite)
    for mensaje in (MENSAJE_LIBERADO, APAGADO_COMPLETO, FIN_DEL_SERVIDOR):
        assert mensaje in salida, (mensaje, salida)
    assert TOPE_VENCIDO not in salida and MENSAJE_CONSUMO_SIN_PARAR not in salida
