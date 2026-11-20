"""HU-W07 (paso 7.6): una tarea que se retira termina sus solicitudes en curso antes de detenerse.

uvicorn corre en un subproceso con exactamente los argumentos del CMD del Dockerfile, leídos del
archivo, salvo la app (apoyo_drenaje.py: dobles sin base ni red, con la identidad del cliente
lenta) y el puerto (uno libre). Recibe SIGTERM, como lo manda ECS al retirar la tarea.

- A: con el valor del Dockerfile, la cotización en curso (unos 3 s) termina en 201, una conexión
  nueva se rechaza y el proceso sale antes del tope, tras completar el apagado de la app.
- B: con el valor reemplazado por 1 y una cotización de unos 5 s, uvicorn la cancela al vencer el
  tope y sale en 1-2 s: el flag se respeta.

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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="SIGTERM no es una parada ordenada en Windows"
)

RAIZ = Path(__file__).resolve().parents[4]
DOCKERFILE = RAIZ / "services" / "cotizacion" / "Dockerfile"
APOYO = Path(__file__).resolve().parent
FLAG = "--timeout-graceful-shutdown"
APP = "cotizacion.main:app"
# uvicorn 0.54.0 (uv.lock): uvicorn/lifespan/on.py y uvicorn/server.py.
APAGADO_COMPLETO = "Application shutdown complete."
ESPERA_CONEXIONES = "Waiting for connections to close."
TOPE_VENCIDO = "running task(s), timeout graceful shutdown exceeded"
FIN_DEL_SERVIDOR = "Finished server process"
# Tras un apagado ordenado por señal, uvicorn restaura el manejador original y vuelve a lanzar la
# señal (uvicorn/server.py, capture_signals): el proceso termina por SIGTERM (143 en el contenedor),
# no con 0. Que el apagado se completó lo prueban los mensajes de arriba.
SALIDA_TRAS_SIGTERM = -signal.SIGTERM
ASESOR = {"X-Actor-Id": "asesor-sintetico", "X-Actor-Rol": "ASESOR"}
C01 = {
    "clienteId": "00000000-0000-4000-8000-000000000001",
    "productoId": "vida-hipotecario",
    "datosAsegurado": {
        "fechaNacimiento": "1990-04-18",
        "plazoCreditoMeses": 180,
        "sumaAsegurada": 180000000,
    },
}


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


def puerto_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        puerto: int = s.getsockname()[1]
    return puerto


def argumentos(puerto: int, tope: str | None = None) -> list[str]:
    """Los del CMD, con otra app y otro puerto (y, si se da, otro tope)."""
    cmd = cmd_del_dockerfile()
    valor_del_dockerfile()
    argumentos = list(cmd[1:])
    argumentos[argumentos.index(APP)] = "apoyo_drenaje:app"
    argumentos[argumentos.index("--port") + 1] = str(puerto)
    if tope is not None:
        argumentos[argumentos.index(FLAG) + 1] = tope
    return [sys.executable, "-m", cmd[0], *argumentos]


@dataclass
class Servidor:
    proceso: subprocess.Popen[str]
    url: str
    lineas: list[str] = field(default_factory=list)

    def salida(self) -> str:
        return "\n".join(self.lineas)


@contextmanager
def servidor(demora_s: float, tope: str | None = None) -> Iterator[Servidor]:
    puerto = puerto_libre()
    entorno = {**os.environ, "PYTHONPATH": str(APOYO), "DRENAJE_DEMORA_S": str(demora_s)}
    entorno.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
    proceso = subprocess.Popen(
        argumentos(puerto, tope),
        cwd=RAIZ,
        env=entorno,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    s = Servidor(proceso, f"http://127.0.0.1:{puerto}")
    assert proceso.stdout is not None
    lector = threading.Thread(target=lambda: s.lineas.extend(proceso.stdout), daemon=True)  # type: ignore[arg-type]
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


@dataclass
class Resultado:
    respuesta: httpx.Response | None = None
    error: BaseException | None = None
    termino: float = 0.0


def cotizar_en_hilo(url: str, clave: str) -> tuple[threading.Thread, Resultado]:
    resultado = Resultado()

    def enviar() -> None:
        try:
            resultado.respuesta = httpx.post(
                f"{url}/v1/cotizaciones",
                json=C01,
                headers={**ASESOR, "Idempotency-Key": clave},
                timeout=30,
            )
        except httpx.HTTPError as error:
            resultado.error = error
        finally:
            resultado.termino = time.monotonic()

    hilo = threading.Thread(target=enviar)
    hilo.start()
    return hilo, resultado


def esperar_salida(s: Servidor, tope_s: float) -> tuple[int, float]:
    inicio = time.monotonic()
    try:
        codigo = s.proceso.wait(timeout=tope_s)
    except subprocess.TimeoutExpired:
        pytest.fail(f"uvicorn no salió en {tope_s} s:\n{s.salida()}")
    return codigo, time.monotonic() - inicio


def test_a_con_el_valor_del_dockerfile_la_solicitud_en_curso_termina() -> None:
    tope = int(valor_del_dockerfile())
    with servidor(demora_s=3) as s:
        hilo, resultado = cotizar_en_hilo(s.url, "00000000-0000-4000-c000-0000000007a1")
        time.sleep(1)  # la cotización ya está esperando a la identidad
        assert resultado.termino == 0.0, "la cotización terminó antes del SIGTERM"
        senal = time.monotonic()
        s.proceso.send_signal(signal.SIGTERM)
        time.sleep(0.5)
        with pytest.raises(httpx.ConnectError):
            httpx.get(f"{s.url}/health", timeout=2)
        codigo, _ = esperar_salida(s, tope)
        duracion = time.monotonic() - senal
        hilo.join(30)
        salida = s.salida()

    print(f"\nHU-W07 drenaje A: salió con {codigo} a los {duracion:.1f} s del SIGTERM\n{salida}")
    assert resultado.error is None, resultado.error
    assert resultado.respuesta is not None and resultado.respuesta.status_code == 201
    assert resultado.termino > senal, "la respuesta llegó después del SIGTERM"
    assert codigo == SALIDA_TRAS_SIGTERM and duracion < tope
    for mensaje in (ESPERA_CONEXIONES, APAGADO_COMPLETO, FIN_DEL_SERVIDOR):
        assert mensaje in salida, (mensaje, salida)
    assert TOPE_VENCIDO not in salida


def test_b_con_tope_de_1_s_uvicorn_corta_la_solicitud() -> None:
    with servidor(demora_s=5, tope="1") as s:
        hilo, resultado = cotizar_en_hilo(s.url, "00000000-0000-4000-c000-0000000007b1")
        time.sleep(1)
        senal = time.monotonic()
        s.proceso.send_signal(signal.SIGTERM)
        codigo, _ = esperar_salida(s, 10)
        duracion = time.monotonic() - senal
        hilo.join(30)
        salida = s.salida()

    recibido: Any = (
        resultado.respuesta.status_code if resultado.respuesta is not None else resultado.error
    )
    print(
        f"\nHU-W07 drenaje B: salió con {codigo} a los {duracion:.1f} s del SIGTERM; "
        f"el cliente recibió {recibido!r}\n{salida}"
    )
    assert duracion < 3, "con tope de 1 s debe salir en 1-2 s"
    assert resultado.termino - senal < 4, "la solicitud de 5 s no llegó a terminar"
    assert resultado.respuesta is None or resultado.respuesta.status_code >= 500
    assert codigo == SALIDA_TRAS_SIGTERM
    for mensaje in (TOPE_VENCIDO, APAGADO_COMPLETO, FIN_DEL_SERVIDOR):
        assert mensaje in salida, (mensaje, salida)
