"""App de cotizacion para la prueba de drenaje (HU-W07, paso 7.6).

La sirve uvicorn en un subproceso, con los argumentos del CMD del Dockerfile
(test_hu_w07_drenaje.py). Los puertos son los dobles de tests/conftest.py: no hay base de datos
ni red externa. La identidad del cliente, una dependencia de la cotización, tarda
DRENAJE_DEMORA_S segundos. Los topes de dependencia y de recorrido se suben para que esa demora
no se corte por timeout.
"""

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

from cotizacion.config import Settings
from cotizacion.main import Puertos, crear_app
from fastapi import FastAPI

DEMORA = "DRENAJE_DEMORA_S"


def _dobles() -> ModuleType:
    """tests/conftest.py, cargado por su ruta: tests/ no es un paquete (--import-mode=importlib)."""
    ruta = Path(__file__).resolve().parents[1] / "conftest.py"
    especificacion = importlib.util.spec_from_file_location("dobles_cotizacion", ruta)
    assert especificacion is not None and especificacion.loader is not None
    modulo = importlib.util.module_from_spec(especificacion)
    sys.modules[especificacion.name] = modulo
    especificacion.loader.exec_module(modulo)
    return modulo


def crear() -> FastAPI:
    from cotizacion.domain.perfilamiento.modelos import EstadoIdentidad

    dobles = _dobles()
    demora_s = float(os.environ.get(DEMORA, "0"))
    puertos = Puertos(
        identidad=dobles.IdentidadFalsa(EstadoIdentidad.VERIFICADO, demora_s=demora_s),
        consentimiento=dobles.ConsentimientoFalso(None),
        modelos=dobles.ModelosFalsos(None),
        ejecutor=dobles.EjecutorFalso(),
        perfiles=dobles.RepositorioPerfilesMemoria(),
        reloj=dobles.RelojFijo(),
        productos=dobles.ProductosFalsos(),
        cotizaciones=dobles.RepositorioCotizacionesMemoria(),
    )
    settings = Settings(dependencia_timeout_ms=60_000, LATENCY_BUDGET_MS={"cotizacion": 120_000})
    return crear_app(settings, puertos=puertos)


app = crear()
