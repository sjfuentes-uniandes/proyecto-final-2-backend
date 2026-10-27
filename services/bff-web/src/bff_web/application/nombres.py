"""Nombre de los clientes de una página (bandeja de suscripción, HU-W13; cotizaciones abiertas).

Los servicios de dominio guardan solo el id del cliente: el portal muestra el nombre que entrega
clientes. Se pide una vez por cliente distinto, en paralelo y con su propio timeout; si clientes no
lo da a tiempo, el nombre es None y la página se muestra igual (el portal pone "no disponible").
"""

import asyncio
from typing import Any
from uuid import UUID

import httpx

from bff_web.adapters.outbound.clientes import ClientesCliente, ErrorClientes


async def nombres_de_clientes(
    clientes: ClientesCliente, ids: list[Any], actor: str, timeout_s: float
) -> dict[str, str | None]:
    unicos = list(dict.fromkeys(str(c) for c in ids if c))
    nombres = await asyncio.gather(*(_nombre(clientes, c, actor, timeout_s) for c in unicos))
    return dict(zip(unicos, nombres, strict=True))


async def _nombre(
    clientes: ClientesCliente, cliente_id: str, actor: str, timeout_s: float
) -> str | None:
    try:
        cliente = await asyncio.wait_for(clientes.obtener(UUID(cliente_id), actor), timeout_s)
    except (TimeoutError, ErrorClientes, httpx.HTTPError, ValueError):
        return None
    nombre = cliente.get("nombreCompleto") if isinstance(cliente, dict) else None
    return nombre if isinstance(nombre, str) else None
