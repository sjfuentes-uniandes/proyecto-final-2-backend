"""Condiciones de la oferta en las reglas de prima (HU-W04): esquema, validación y semillas.

El bloque `oferta` es opcional y aditivo: las reglas sin él (2026.09.1) se serializan igual que
antes, así que su hash publicado se sigue verificando desde la `definicion`.
"""

import copy
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Self

import pytest
from catalogo.adapters.inbound.http.esquema_prima import DefinicionReglasPrima, a_dict
from catalogo.adapters.outbound import productos_pg
from catalogo.adapters.outbound.productos_pg import PRODUCTOS_QA, sembrar, sembrar_qa, semillas
from catalogo.domain.productos.producto import Producto
from catalogo.domain.productos.validacion import validar_reglas
from catalogo.domain.reglas.hash import hash_canonico
from pydantic import ValidationError


def _vigentes() -> tuple[Producto, dict[str, Any]]:
    producto, reglas = semillas()[0]
    version, _, definicion = reglas[-1]
    assert version == "2026.10.1"
    return producto, copy.deepcopy(definicion)


def test_la_version_2026_10_1_trae_las_condiciones_de_la_oferta() -> None:
    producto, definicion = _vigentes()
    _, reglas = semillas()[0]
    assert [(v, d) for v, d, _ in reglas] == [
        ("2026.09.1", datetime(2026, 9, 1, tzinfo=UTC)),
        ("2026.10.1", datetime(2026, 10, 22, tzinfo=UTC)),
    ]
    oferta = definicion["oferta"]
    assert oferta["vigenciaMinutos"] == 7 * 24 * 60
    assert len(oferta["exclusiones"]) == 4
    assert {s["cobertura"]: s["porcentaje"] for s in oferta["sumasCoberturas"]} == {
        "ENFERMEDADES_GRAVES": "0.30",
        "AUXILIO_FUNERARIO": "0.03",
    }
    # Las tarifas no cambian: solo se agregan las condiciones de la oferta.
    anterior = copy.deepcopy(semillas()[0][1][0][2])
    assert {k: v for k, v in definicion.items() if k != "oferta"} == anterior
    assert validar_reglas(definicion, producto) == ()


def test_sin_oferta_la_clave_no_se_serializa_y_el_hash_no_cambia() -> None:
    anterior = semillas()[0][1][0][2]
    assert "oferta" not in anterior
    validada = DefinicionReglasPrima.model_validate(anterior)
    assert validada.oferta is None
    assert "oferta" not in validada.model_dump(by_alias=True)
    assert "oferta" not in validada.model_dump_json(by_alias=True)
    assert hash_canonico(a_dict(validada)) == hash_canonico(anterior)


CASOS: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
    (
        "exclusión repetida",
        lambda o: o["exclusiones"].append(copy.deepcopy(o["exclusiones"][0])),
        "EXCLUSION_DUPLICADA:SUICIDIO_PRIMER_ANIO",
    ),
    (
        "suma repetida",
        lambda o: o["sumasCoberturas"].append(copy.deepcopy(o["sumasCoberturas"][0])),
        "SUMA_DUPLICADA:ENFERMEDADES_GRAVES",
    ),
    (
        "suma de una cobertura ajena",
        lambda o: o["sumasCoberturas"].append({"cobertura": "ROBO", "porcentaje": "0.5"}),
        "SUMA_SIN_COBERTURA:ROBO",
    ),
    (
        "porcentaje 0",
        lambda o: o["sumasCoberturas"][0].update(porcentaje="0"),
        "SUMA_FUERA_DE_RANGO:ENFERMEDADES_GRAVES",
    ),
    (
        "porcentaje mayor que 1",
        lambda o: o["sumasCoberturas"][0].update(porcentaje="1.01"),
        "SUMA_FUERA_DE_RANGO:ENFERMEDADES_GRAVES",
    ),
]


@pytest.mark.parametrize(("caso", "cambio", "codigo"), CASOS, ids=[c[0] for c in CASOS])
def test_cada_regla_de_la_oferta_rechaza_su_caso(
    caso: str, cambio: Callable[[dict[str, Any]], None], codigo: str
) -> None:
    producto, definicion = _vigentes()
    cambio(definicion["oferta"])
    assert validar_reglas(definicion, producto) == (codigo,)


def test_el_porcentaje_1_es_valido() -> None:
    producto, definicion = _vigentes()
    definicion["oferta"]["sumasCoberturas"][0]["porcentaje"] = "1"
    assert validar_reglas(definicion, producto) == ()


@pytest.mark.parametrize(
    ("cambio", "campo"),
    [
        (lambda o: o.update(vigenciaMinutos=0), "vigenciaMinutos"),
        (lambda o: o.update(vigenciaMinutos=525_601), "vigenciaMinutos"),
        (lambda o: o.pop("sumasCoberturas"), "sumasCoberturas"),
        (lambda o: o["exclusiones"][0].update(codigo="minúsculas"), "codigo"),
        (lambda o: o["exclusiones"][0].update(descripcion=""), "descripcion"),
        (lambda o: o["exclusiones"].extend([o["exclusiones"][0]] * 20), "exclusiones"),
        (lambda o: o.update(extra=1), "extra"),
    ],
)
def test_el_esquema_de_la_oferta_rechaza_la_forma(
    cambio: Callable[[dict[str, Any]], None], campo: str
) -> None:
    _, definicion = _vigentes()
    cambio(definicion["oferta"])
    with pytest.raises(ValidationError, match=campo):
        DefinicionReglasPrima.model_validate(definicion)


def test_los_limites_de_la_vigencia_son_validos() -> None:
    _, definicion = _vigentes()
    for minutos in (1, 525_600):
        definicion["oferta"]["vigenciaMinutos"] = minutos
        assert DefinicionReglasPrima.model_validate(definicion).oferta is not None


def test_la_semilla_qa_trae_una_oferta_de_un_minuto() -> None:
    (producto, reglas), *otros = semillas(PRODUCTOS_QA)
    assert otros == []
    assert producto.id == "vida-hipotecario-qa-oferta-corta"
    (version, _, definicion), *_ = reglas
    assert version == "2026.10.1"
    assert definicion["oferta"]["vigenciaMinutos"] == 1
    assert validar_reglas(definicion, producto) == ()
    # No se publica con la semilla normal.
    assert producto.id not in {p.id for p, _ in semillas()}


class ConexionSincrona:
    def __init__(self) -> None:
        self.sentencias: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, parametros: tuple[Any, ...]) -> None:
        self.sentencias.append((sql, parametros))

    def transaction(self) -> Self:
        return self

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def test_sembrar_qa_publica_solo_los_productos_de_qa(monkeypatch: pytest.MonkeyPatch) -> None:
    conexion = ConexionSincrona()
    urls: list[str] = []

    def conectar(url: str) -> ConexionSincrona:
        urls.append(url)
        return conexion

    monkeypatch.setattr(productos_pg.psycopg, "connect", conectar)
    sembrar_qa("postgresql://qa")
    productos = [p[0] for s, p in conexion.sentencias if "INSERT INTO productos" in s]
    assert (urls, productos) == (["postgresql://qa"], ["vida-hipotecario-qa-oferta-corta"])


def test_sembrar_publica_las_dos_versiones_de_vida_hipotecario() -> None:
    conexion = ConexionSincrona()
    sembrar(conexion)  # type: ignore[arg-type]
    reglas = [p for s, p in conexion.sentencias if "INSERT INTO reglas_prima" in s]
    versiones = [(p[0], p[1]) for p in reglas]
    assert versiones == [
        ("vida-hipotecario", "2026.09.1"),
        ("vida-hipotecario", "2026.10.1"),
        ("vida-hipotecario-2025", "2025.01.1"),
    ]
    for _, _, _, definicion, hash_, _ in reglas:
        assert hash_ == hash_canonico(definicion.obj)


@pytest.mark.parametrize("productos_qa", [True, False])
def test_el_arranque_publica_los_productos_qa_solo_si_se_piden(
    monkeypatch: pytest.MonkeyPatch, productos_qa: bool
) -> None:
    import psycopg_pool
    from catalogo.adapters.outbound import modelos_pg
    from catalogo.config import Settings
    from catalogo.main import crear_app
    from fastapi.testclient import TestClient

    sembradas: list[str] = []
    monkeypatch.setattr(modelos_pg, "migrar", lambda url: None)
    monkeypatch.setattr(productos_pg, "sembrar_qa", sembradas.append)
    monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", PoolFalso)
    settings = Settings(DB_HOST="rds.interno", CATALOGO_PRODUCTOS_QA=productos_qa)
    with TestClient(crear_app(settings)):
        pass
    assert sembradas == ([settings.database_url] if productos_qa else [])


class PoolFalso:
    def __init__(self, url: str, **argumentos: Any) -> None:
        self.url = url

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None
