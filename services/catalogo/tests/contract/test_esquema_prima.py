"""El esquema publicado de las reglas de prima (contracts/modelos/reglas-prima.v1.schema.json) es
el que genera el código, y el valor que catalogo deja en la caché es la respuesta de su API
(cotizacion lo lee con la misma copia de los DTO, HU-W03)."""

import json
from pathlib import Path
from typing import Any

import pytest
from catalogo.adapters.inbound.http.esquema_prima import DefinicionReglasPrima, a_dict
from catalogo.adapters.outbound.productos_cache import (
    ProductosConCache,
    clave_producto,
    clave_reglas_vigentes,
)
from catalogo.adapters.outbound.productos_pg import semillas
from catalogo.main import app
from fastapi.testclient import TestClient
from pydantic import ValidationError

ESQUEMA = Path(__file__).parents[4] / "contracts" / "modelos" / "reglas-prima.v1.schema.json"
pytestmark = pytest.mark.anyio


def test_esquema_publicado_coincide_con_el_generado() -> None:
    publicado = json.loads(ESQUEMA.read_text(encoding="utf-8"))
    assert publicado == DefinicionReglasPrima.model_json_schema()


def test_las_semillas_cumplen_el_esquema() -> None:
    for _, reglas in semillas():
        for _, _, definicion in reglas:
            assert a_dict(DefinicionReglasPrima.model_validate(definicion)) == definicion


def test_un_campo_extra_se_rechaza() -> None:
    definicion = dict(semillas()[0][1][0][2])
    definicion["extra"] = 1
    with pytest.raises(ValidationError):
        DefinicionReglasPrima.model_validate(definicion)


class CacheMemoria:
    def __init__(self) -> None:
        self.valores: dict[str, bytes] = {}

    async def leer(self, clave: str) -> bytes | None:
        return self.valores.get(clave)

    async def escribir(self, clave: str, valor: bytes, ttl_s: int) -> None:
        self.valores[clave] = valor

    async def borrar(self, *claves: str) -> None:
        return None

    async def cerrar(self) -> None:
        return None


async def test_el_valor_en_cache_es_la_respuesta_de_la_api(
    api: TestClient, productos_repo: Any, reloj: Any
) -> None:
    cache = CacheMemoria()
    con_cache = ProductosConCache(productos_repo, cache, reloj, ttl_s=60)
    await con_cache.producto("vida-hipotecario")
    await con_cache.reglas_vigentes("vida-hipotecario", reloj.ahora())
    producto = api.get("/v1/productos/vida-hipotecario").json()
    reglas = api.get("/v1/productos/vida-hipotecario/reglas-prima/vigente").json()
    assert json.loads(cache.valores[clave_producto("vida-hipotecario")]) == producto
    assert json.loads(cache.valores[clave_reglas_vigentes("vida-hipotecario")]) == reglas
    # Y lo publica el OpenAPI de catalogo.
    esquemas = app.openapi()["components"]["schemas"]
    assert set(esquemas["ProductoRespuesta"]["properties"]) == set(producto)
    assert set(esquemas["ReglasPrimaRespuesta"]["properties"]) == set(reglas)
