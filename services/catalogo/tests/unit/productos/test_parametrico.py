"""Producto paramétrico de QA `clima-lluvia` y su bloque opcional `parametrico` (HU-W21, W21-B04).

El producto lo publica la semilla de QA (`CATALOGO_PRODUCTOS_QA`); el bloque es aditivo: los demás
productos no lo traen y su respuesta no cambia.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from catalogo.adapters.inbound.http.productos import ProductoRespuesta
from catalogo.adapters.outbound import productos_pg
from catalogo.adapters.outbound.productos_cache import ProductosConCache, clave_producto
from catalogo.adapters.outbound.productos_pg import PRODUCTOS_QA, sembrar, semillas
from catalogo.domain.productos.errores import ReglasInvalidas
from catalogo.domain.productos.producto import Parametrico, Producto, ReglasPrima
from catalogo.domain.productos.validacion import validar_parametrico, validar_reglas
from catalogo.domain.reglas.hash import hash_canonico
from fastapi.testclient import TestClient

pytestmark = pytest.mark.anyio

DATOS = Path(__file__).parents[5] / "contracts" / "datos-sinteticos" / "eventos-parametricos.json"


def _clima_lluvia() -> tuple[Producto, list[tuple[str, datetime, dict[str, Any]]]]:
    return next((p, r) for p, r in semillas(PRODUCTOS_QA) if p.id == "clima-lluvia")


class ProductosMemoria:
    """RepositorioProductos con la semilla normal y la de QA (el `conftest` solo trae la normal)."""

    def __init__(self) -> None:
        self.productos: dict[str, Producto] = {}
        self.reglas: list[ReglasPrima] = []
        for producto, reglas in [*semillas(), *semillas(PRODUCTOS_QA)]:
            self.productos[producto.id] = producto
            self.reglas.extend(
                ReglasPrima(producto.id, v, desde, d, hash_canonico(d), "migracion")
                for v, desde, d in reglas
            )

    async def producto(self, producto_id: str) -> Producto | None:
        return self.productos.get(producto_id)

    async def reglas_vigentes(self, producto_id: str, en: datetime) -> ReglasPrima | None:
        candidatas = [
            r for r in self.reglas if r.producto_id == producto_id and r.vigente_desde <= en
        ]
        return max(candidatas, key=lambda r: r.vigente_desde, default=None)

    async def reglas_por_version(self, producto_id: str, version: str) -> ReglasPrima | None:
        return next(
            (r for r in self.reglas if r.producto_id == producto_id and r.version == version),
            None,
        )


@pytest.fixture
def productos_repo() -> ProductosMemoria:
    return ProductosMemoria()


# --- La semilla ----------------------------------------------------------------------------------
def test_la_semilla_de_qa_trae_clima_lluvia_con_su_regla() -> None:
    producto, reglas = _clima_lluvia()
    assert (producto.ramo, producto.pais, producto.moneda) == ("PARAMETRICO", "CO", "COP")
    assert producto.estado.value == "ACTIVO"
    assert producto.parametrico == Parametrico(
        tipo_evento="LLUVIA_ACUMULADA",
        unidad="mm",
        umbral="120",
        valor_pago="1400000.00",
        version_regla="1",
    )
    assert [c.codigo for c in producto.coberturas] == ["LLUVIA_ACUMULADA"]
    assert validar_parametrico(producto) == ()
    (version, _, definicion), *otras = reglas
    assert (version, otras) == ("2026.11.1", [])
    assert validar_reglas(definicion, producto) == ()


def test_clima_lluvia_solo_se_publica_con_la_semilla_de_qa() -> None:
    assert "clima-lluvia" not in {p.id for p, _ in semillas()}
    assert "clima-lluvia" in {p.id for p, _ in semillas(PRODUCTOS_QA)}


def test_el_resto_de_los_productos_no_trae_el_bloque() -> None:
    assert all(p.parametrico is None for p, _ in semillas())
    assert [p.parametrico for p, _ in semillas(PRODUCTOS_QA) if p.id != "clima-lluvia"] == [None]


def test_la_prima_de_la_semilla_es_la_de_las_polizas_sinteticas() -> None:
    """La prima mensual (tasa × suma asegurada) y la regla coinciden con `eventos-parametricos.json`
    (PolizaEmitida con `coberturaParametrica`): el producto y los datos de R5 no se separan."""
    producto, reglas = _clima_lluvia()
    definicion = reglas[0][2]
    suma = definicion["limites"]["sumaAsegurada"]["maximo"]
    sobre = json.loads(DATOS.read_text(encoding="utf-8"))["coberturas"][0]["sobre"]["payload"]
    assert sobre["producto"] == {"id": producto.id, "version": producto.version}
    assert sobre["ramo"]["nombre"] == "Paramétrico"
    assert sobre["sumaAsegurada"] == f"{suma}.00"
    assert f"{float(definicion['tasaBase']) * float(suma):.2f}" == sobre["prima"]
    regla = sobre["coberturaParametrica"]
    assert producto.parametrico is not None
    assert (regla["tipoEvento"], regla["unidad"], regla["umbral"], regla["valorPago"]) == (
        producto.parametrico.tipo_evento,
        producto.parametrico.unidad,
        producto.parametrico.umbral,
        producto.parametrico.valor_pago,
    )
    assert regla["versionRegla"] == producto.parametrico.version_regla


class ConexionSincrona:
    def __init__(self) -> None:
        self.sentencias: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, parametros: tuple[Any, ...]) -> None:
        self.sentencias.append((sql, parametros))


def test_sembrar_guarda_el_bloque_solo_del_producto_parametrico() -> None:
    conexion = ConexionSincrona()
    sembrar(conexion, PRODUCTOS_QA)  # type: ignore[arg-type]
    filas = {
        p[0]: p[10]
        for s, p in conexion.sentencias
        if "INSERT INTO productos" in s and "parametrico" in s
    }
    assert filas["vida-hipotecario-qa-oferta-corta"] is None
    assert filas["clima-lluvia"].obj == {
        "tipoEvento": "LLUVIA_ACUMULADA",
        "unidad": "mm",
        "umbral": "120",
        "valorPago": "1400000.00",
        "versionRegla": "1",
    }


# --- La validación -------------------------------------------------------------------------------
def _con(**cambios: str) -> Producto:
    producto, _ = _clima_lluvia()
    assert producto.parametrico is not None
    return replace(producto, parametrico=replace(producto.parametrico, **cambios))


def test_un_producto_sin_bloque_es_valido() -> None:
    producto, _ = _clima_lluvia()
    assert validar_parametrico(replace(producto, parametrico=None)) == ()


@pytest.mark.parametrize(
    ("cambios", "codigos"),
    [
        (
            {"tipo_evento": "GRANIZO"},
            ("PARAMETRICO_TIPO_EVENTO_DESCONOCIDO", "PARAMETRICO_SIN_COBERTURA"),
        ),
        ({"unidad": "min"}, ("PARAMETRICO_UNIDAD_INCOHERENTE",)),
        ({"umbral": "0"}, ("PARAMETRICO_UMBRAL_INVALIDO",)),
        ({"umbral": "-1"}, ("PARAMETRICO_UMBRAL_INVALIDO",)),
        ({"umbral": "120,5"}, ("PARAMETRICO_UMBRAL_INVALIDO",)),
        ({"umbral": "1e3"}, ("PARAMETRICO_UMBRAL_INVALIDO",)),
        ({"umbral": "120.0001"}, ("PARAMETRICO_UMBRAL_INVALIDO",)),
        ({"umbral": "Infinity"}, ("PARAMETRICO_UMBRAL_INVALIDO",)),
        ({"umbral": "١٢٠"}, ("PARAMETRICO_UMBRAL_INVALIDO",)),
        ({"valor_pago": "0.00"}, ("PARAMETRICO_VALOR_PAGO_INVALIDO",)),
        ({"valor_pago": "1400000.001"}, ("PARAMETRICO_VALOR_PAGO_INVALIDO",)),
        ({"valor_pago": "mucho"}, ("PARAMETRICO_VALOR_PAGO_INVALIDO",)),
        ({"version_regla": "  "}, ("PARAMETRICO_VERSION_REGLA_VACIA",)),
    ],
)
def test_cada_regla_incumplida_tiene_su_codigo(
    cambios: dict[str, str], codigos: tuple[str, ...]
) -> None:
    assert validar_parametrico(_con(**cambios)) == codigos


def test_la_cobertura_que_paga_debe_ser_del_producto() -> None:
    producto, _ = _clima_lluvia()
    sin_cobertura = replace(producto, coberturas=())
    assert validar_parametrico(sin_cobertura) == ("PARAMETRICO_SIN_COBERTURA",)


def test_el_retraso_de_vuelo_va_en_minutos_y_su_cobertura_es_la_suya() -> None:
    # En minutos es coherente, pero el producto no tiene la cobertura RETRASO_VUELO.
    assert validar_parametrico(_con(tipo_evento="RETRASO_VUELO", unidad="min")) == (
        "PARAMETRICO_SIN_COBERTURA",
    )
    # En milímetros ni la unidad ni la cobertura corresponden.
    assert validar_parametrico(_con(tipo_evento="RETRASO_VUELO")) == (
        "PARAMETRICO_UNIDAD_INCOHERENTE",
        "PARAMETRICO_SIN_COBERTURA",
    )


def test_una_semilla_con_bloque_invalido_impide_arrancar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    producto = json.loads(
        productos_pg._SEMILLAS.joinpath(PRODUCTOS_QA).read_text(encoding="utf-8")
    )[1]
    producto["parametrico"]["umbral"] = "0"
    (tmp_path / "productos.json").write_text(json.dumps([producto]), encoding="utf-8")
    monkeypatch.setattr(productos_pg, "_SEMILLAS", tmp_path)
    with pytest.raises(ReglasInvalidas) as error:
        semillas("productos.json")
    assert error.value.errores == ("PARAMETRICO_UMBRAL_INVALIDO",)


# --- La API y la caché ---------------------------------------------------------------------------
def test_la_api_entrega_el_bloque_del_producto_parametrico(api: TestClient) -> None:
    cuerpo = api.get("/v1/productos/clima-lluvia").json()
    assert cuerpo["ramo"] == "PARAMETRICO"
    assert cuerpo["parametrico"] == {
        "tipoEvento": "LLUVIA_ACUMULADA",
        "unidad": "mm",
        "umbral": "120",
        "valorPago": "1400000.00",
        "versionRegla": "1",
    }


def test_la_api_de_los_demas_productos_no_cambia(api: TestClient) -> None:
    """Sin el bloque, la clave no se serializa: la respuesta es la de antes de HU-W21."""
    assert "parametrico" not in api.get("/v1/productos/vida-hipotecario").json()


def test_el_dto_omite_el_bloque_nulo() -> None:
    for producto, _ in semillas():
        dto = ProductoRespuesta.desde(producto).model_dump(mode="json", by_alias=True)
        assert "parametrico" not in dto


async def test_la_cache_conserva_el_bloque(productos_repo: ProductosMemoria) -> None:
    class Cache:
        def __init__(self) -> None:
            self.valores: dict[str, bytes] = {}

        async def leer(self, clave: str) -> bytes | None:
            return self.valores.get(clave)

        async def escribir(self, clave: str, valor: bytes, ttl_s: int) -> None:
            self.valores[clave] = valor

        async def borrar(self, *claves: str) -> None:
            for clave in claves:
                self.valores.pop(clave, None)

        async def cerrar(self) -> None:
            return None

    class Reloj:
        def ahora(self) -> datetime:
            return datetime(2026, 11, 11, tzinfo=UTC)

    cache = Cache()
    con_cache = ProductosConCache(productos_repo, cache, Reloj(), ttl_s=60)  # type: ignore[arg-type]
    primero = await con_cache.producto("clima-lluvia")
    assert "parametrico" in json.loads(cache.valores[clave_producto("clima-lluvia")])
    segundo = await con_cache.producto("clima-lluvia")
    assert primero is not None and primero.parametrico is not None
    assert segundo == primero

    # Un valor guardado antes de HU-W21 (sin la clave) sigue siendo válido.
    antiguo = json.loads(cache.valores[clave_producto("clima-lluvia")])
    del antiguo["parametrico"]
    cache.valores[clave_producto("clima-lluvia")] = json.dumps(antiguo).encode()
    leido = await con_cache.producto("clima-lluvia")
    assert leido is not None and leido.parametrico is None
