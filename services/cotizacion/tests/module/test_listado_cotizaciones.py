"""N2 del listado de cotizaciones con oferta ("Cotizaciones abiertas" del portal) sobre PostgreSQL,
catalogo y clientes efímeros (arnés de conftest.py): orden, cursor, estado derivado, búsqueda por
código e índices de la migración 006. La base es de la sesión: cada prueba se aísla por código o
por ser la más reciente."""

from collections.abc import Callable
from typing import Any
from uuid import uuid4

import psycopg
import pytest

pytest.importorskip("docker")

ASESOR = {"X-Actor-Id": "asesor-sintetico", "X-Actor-Rol": "ASESOR"}
SOLICITUD = {
    "clienteId": "00000000-0000-4000-8000-000000000001",
    "productoId": "vida-hipotecario",
    "datosAsegurado": {
        "fechaNacimiento": "1990-04-18",
        "plazoCreditoMeses": 180,
        "sumaAsegurada": 180000000,
    },
}


def _cotizar(app: Any) -> dict[str, Any]:
    respuesta = app.cliente.post(
        "/v1/cotizaciones", json=SOLICITUD, headers={**ASESOR, "Idempotency-Key": str(uuid4())}
    )
    assert respuesta.status_code == 201, respuesta.text
    oferta: dict[str, Any] = respuesta.json()["oferta"]
    return oferta


def _listar(app: Any, **parametros: Any) -> dict[str, Any]:
    respuesta = app.cliente.get("/v1/cotizaciones", params=parametros, headers=ASESOR)
    assert respuesta.status_code == 200, respuesta.text
    cuerpo: dict[str, Any] = respuesta.json()
    return cuerpo


def test_de_la_mas_reciente_a_la_mas_antigua_por_cursor(cotizacion: Callable[..., Any]) -> None:
    app = cotizacion()
    codigos = [_cotizar(app)["codigo"] for _ in range(3)]
    primera = _listar(app, limite=2)
    assert [i["codigo"] for i in primera["items"]] == codigos[:0:-1]
    assert primera["total"] >= 3 and primera["siguienteCursor"]
    segunda = _listar(app, limite=2, cursor=primera["siguienteCursor"])
    assert segunda["items"][0]["codigo"] == codigos[0]
    item = primera["items"][0]
    assert (item["prima"], item["moneda"], item["estado"]) == ("82593", "COP", "VIGENTE")
    assert (item["sumaAsegurada"], item["nivelRiesgo"], item["degradada"]) == (
        "180000000",
        None,
        False,
    )


def test_estado_y_busqueda_por_codigo(cotizacion: Callable[..., Any]) -> None:
    app = cotizacion()
    oferta = _cotizar(app)
    vencida = _vencer(app, oferta)
    por_codigo = _listar(app, q=oferta["codigo"])
    # El código de la vencida es el de la original con un dígito más: la búsqueda es por parte.
    assert {i["codigo"]: i["estado"] for i in por_codigo["items"]} == {
        oferta["codigo"]: "VIGENTE",
        vencida: "VENCIDA",
    }
    assert [i["codigo"] for i in _listar(app, q=vencida, estado="VIGENTE")["items"]] == []
    assert [i["codigo"] for i in _listar(app, q=vencida, estado="VENCIDA")["items"]] == [vencida]
    assert _listar(app, q=oferta["codigo"], estado="VENCIDA")["total"] == 1


def test_un_cursor_ajeno_es_422(cotizacion: Callable[..., Any]) -> None:
    respuesta = cotizacion().cliente.get(
        "/v1/cotizaciones", params={"cursor": "no-es-un-cursor"}, headers=ASESOR
    )
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (422, "SOLICITUD_INVALIDA")


def test_la_migracion_006_crea_los_indices(cotizacion: Callable[..., Any]) -> None:
    app = cotizacion()
    indices = {
        fila[0]
        for fila in app.bases.filas(
            "SELECT indexname FROM pg_indexes WHERE tablename IN ('ofertas', 'cotizaciones')"
        )
    }
    assert {
        "ofertas_creada_en_cotizacion",
        "ofertas_vigente_hasta",
        "cotizaciones_socio",
    } <= indices


def _vencer(app: Any, oferta: dict[str, Any]) -> str:
    """Copia la cotización con su oferta ya vencida (las filas son inmutables: no se puede adelantar
    la vigencia de la original). Devuelve el código de la copia."""
    nueva, nueva_oferta = str(uuid4()), str(uuid4())
    columnas = (
        "cliente_id, socio_id, producto_id, version_producto, moneda, periodicidad, "
        "version_reglas, hash_reglas, perfil_id, nivel_riesgo, entradas, coberturas, desglose, "
        "prima_total, huella_calculo, estado, huella_solicitud, calculado_en, duracion_ms, "
        "correlation_id, actor_id, actor_rol"
    )
    with psycopg.connect(app.bases.cotizacion, autocommit=True) as conexion:
        conexion.execute(
            f"INSERT INTO cotizaciones (id, idempotency_key, {columnas}) "
            f"SELECT %s, %s, {columnas} FROM cotizaciones WHERE id = %s",
            (nueva, str(uuid4()), oferta["cotizacionId"]),
        )
        conexion.execute(
            "INSERT INTO ofertas (id, codigo, cotizacion_id, precio, moneda, periodicidad, "
            "coberturas, exclusiones, version, version_reglas, creada_en, vigente_hasta) "
            "SELECT %s, codigo || '9', %s, precio, moneda, periodicidad, coberturas, exclusiones, "
            "version, version_reglas, creada_en - interval '8 days', creada_en - interval '1 day' "
            "FROM ofertas WHERE id = %s",
            (nueva_oferta, nueva, oferta["id"]),
        )
    return f"{oferta['codigo']}9"
