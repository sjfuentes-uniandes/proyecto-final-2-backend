"""Inspección (ADR 0013, decisión 6): ningún dato de tarjeta ni de cuenta en el adaptador.

Recorre el OpenAPI que sirve la app, el publicado en contracts/, los modelos de dominio, los DTO
de la API y el cuerpo que se envía al proveedor, y falla ante un nombre de campo de tarjeta o de
cuenta.
"""

import asyncio
import dataclasses
import json
import re
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
import yaml
from adaptador_pagos.adapters.inbound.http import cobros
from adaptador_pagos.adapters.outbound.pagos_http import PagosHttp
from adaptador_pagos.domain.cobros import modelos
from adaptador_pagos.main import app
from pydantic import BaseModel, SecretStr

CONTRATO = Path(__file__).parents[4] / "contracts" / "openapi" / "adaptador-pagos.yaml"

# Subcadenas de nombres de campo (normalizados: minúsculas, sin separadores).
SUBCADENAS = (
    "tarjeta",
    "card",
    "cvv",
    "cvc",
    "cuenta",
    "account",
    "iban",
    "swift",
    "bancari",
    "banco",
    "bank",
    "expir",
    "vencimiento",
    "titular",
    "holder",
)
# Nombres que como subcadena darían falsos positivos (`pan` en `company`).
EXACTOS = {"pan", "pin", "nip", "numero", "number", "cbu", "clabe", "routing"}


def _prohibido(nombre: str) -> bool:
    normalizado = re.sub(r"[^a-z0-9]", "", nombre.lower())
    return normalizado in EXACTOS or any(parte in normalizado for parte in SUBCADENAS)


def _nombres_openapi(nodo: Any) -> Iterator[str]:
    if isinstance(nodo, dict):
        for clave, valor in nodo.items():
            if clave == "properties" and isinstance(valor, dict):
                yield from valor
            if clave == "schemas" and isinstance(valor, dict):
                yield from valor
            if clave == "parameters" and isinstance(valor, list):
                yield from (p["name"] for p in valor if isinstance(p, dict) and "name" in p)
            yield from _nombres_openapi(valor)
    elif isinstance(nodo, list):
        for elemento in nodo:
            yield from _nombres_openapi(elemento)


def _nombres_dominio() -> Iterator[str]:
    for objeto in vars(modelos).values():
        if dataclasses.is_dataclass(objeto) and isinstance(objeto, type):
            yield objeto.__name__
            yield from (campo.name for campo in dataclasses.fields(objeto))


def _nombres_dto() -> Iterator[str]:
    for objeto in vars(cobros).values():
        if isinstance(objeto, type) and issubclass(objeto, BaseModel) and objeto is not BaseModel:
            yield objeto.__name__
            for nombre, campo in objeto.model_fields.items():
                yield nombre
                if campo.alias:
                    yield campo.alias


def _nombres_hacia_el_proveedor() -> list[str]:
    enviados: list[dict[str, Any]] = []

    def manejador(request: httpx.Request) -> httpx.Response:
        enviados.append(json.loads(request.content))
        clave = enviados[-1]["referencia"]
        return httpx.Response(
            200,
            json={"referencia": clave, "estado": "APROBADO", "transaccionId": "TX", "motivo": None},
        )

    proveedor = PagosHttp(
        httpx.AsyncClient(transport=httpx.MockTransport(manejador)), "http://sim", SecretStr("k")
    )
    solicitud = modelos.SolicitudCobro(
        clave_recaudo=UUID("7d3f6a2e-0c1b-4b8e-9f00-000000000a01"),
        monto=Decimal("1.00"),
        moneda="COP",
        pagador_ref=UUID("00000000-0000-4000-8000-000000000001"),
    )
    asyncio.run(proveedor.cobrar(solicitud, 1.0))
    return list(enviados[0])


FUENTES = {
    "openapi-servido": lambda: list(_nombres_openapi(app.openapi())),
    "openapi-publicado": lambda: list(
        _nombres_openapi(yaml.safe_load(CONTRATO.read_text(encoding="utf-8")))
    ),
    "dominio": lambda: list(_nombres_dominio()),
    "dto": lambda: list(_nombres_dto()),
    "proveedor": _nombres_hacia_el_proveedor,
}


@pytest.mark.parametrize("fuente", FUENTES)
def test_sin_campos_de_tarjeta_ni_de_cuenta(fuente: str) -> None:
    nombres = FUENTES[fuente]()
    assert nombres, "la inspección no encontró nombres que revisar"
    assert [n for n in nombres if _prohibido(n)] == []


@pytest.mark.parametrize(
    "nombre",
    [
        "numeroTarjeta",
        "card_number",
        "cvv",
        "PAN",
        "numeroCuenta",
        "accountId",
        "iban",
        "fechaExpiracion",
    ],
)
def test_el_detector_reconoce_los_nombres_prohibidos(nombre: str) -> None:
    assert _prohibido(nombre)


@pytest.mark.parametrize(
    "nombre", ["claveRecaudo", "monto", "moneda", "pagadorRef", "transaccionId", "referencia"]
)
def test_el_detector_acepta_los_campos_del_contrato(nombre: str) -> None:
    assert not _prohibido(nombre)
