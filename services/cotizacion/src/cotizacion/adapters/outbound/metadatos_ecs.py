"""Metadatos de la tarea de ECS (HU-W07): el id de la tarea para el span `cotizacion`.

Fargate inyecta `ECS_CONTAINER_METADATA_URI_V4` en cada contenedor; `GET <uri>/task` responde un
JSON con `TaskARN` (`arn:aws:ecs:<región>:<cuenta>:task/<clúster>/<id>`). Se lee una vez al
arrancar (`main.ciclo`), con un tope total de `TIMEOUT_S` y sin reintentos. Cualquier falla da None:
la cotización funciona igual, sin el atributo. La lectura no deja spans: sin solicitud padre, el
GET de httpx (instrumentado de forma global) sería una traza suelta por tarea en X-Ray.
"""

import asyncio
import logging

import httpx
from opentelemetry.instrumentation.utils import suppress_instrumentation

TIMEOUT_S = 1.0


async def leer_task_id(uri: str | None, *, timeout_s: float = TIMEOUT_S) -> str | None:
    """El último segmento de `TaskARN`, o None. Nunca lanza."""
    if not uri or not uri.strip():
        return None
    try:
        async with asyncio.timeout(timeout_s):
            with suppress_instrumentation():
                async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as cliente:
                    respuesta = await cliente.get(f"{uri.strip().rstrip('/')}/task")
            respuesta.raise_for_status()
            datos = respuesta.json()
    except Exception as error:  # noqa: BLE001 - el arranque nunca depende de los metadatos
        # Solo el tipo: sin la URI ni la respuesta.
        logging.getLogger(__name__).warning(
            "metadatos de ECS no disponibles", extra={"error_type": type(error).__name__}
        )
        return None
    arn = datos.get("TaskARN") if isinstance(datos, dict) else None
    if not isinstance(arn, str) or "/" not in arn:
        return None
    return arn.rsplit("/", 1)[-1] or None
