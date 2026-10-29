"""N2 HU-W04: la oferta con PostgreSQL, catalogo y clientes efímeros (arnés de conftest.py), y el
canal del socio de punta a punta: api-socios real (socios en su propia base) → cotizacion servida
con uvicorn → catalogo y clientes.

- AC1: la oferta se guarda con la cotización (código legible, precio, coberturas con su suma,
  exclusiones de las reglas 2026.10.1 y vigencia de 7 días) y el socio la recibe con la lista
  blanca del contrato v1.
- AC3: una oferta vencida se sigue leyendo en cotizacion (200 VENCIDA) y el socio recibe 410
  OFERTA_VENCIDA con detalles.accion = NUEVA_COTIZACION.
- Seguridad: un socio no lee la oferta de otro (404) y las filas de ofertas no cambian. Los logs y
  spans de la suite se revisan al final (test_zz_centinelas.py).
"""

import re
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("docker")

ASESOR = {"X-Actor-Id": "asesor-sintetico", "X-Actor-Rol": "ASESOR"}
C01 = "00000000-0000-4000-8000-000000000001"
DATOS_C01 = {"fechaNacimiento": "1990-04-18", "plazoCreditoMeses": 180, "sumaAsegurada": 180000000}
SOLICITUD = {
    "clienteId": C01,
    "productoId": "vida-hipotecario",
    "datosAsegurado": DATOS_C01,
    "coberturasOpcionales": ["ENFERMEDADES_GRAVES"],
}
CODIGO = re.compile(r"^COT-\d{4}-\d{5,}$")
CAMPOS_V1 = {
    "id",
    "codigo",
    "cotizacionId",
    "productoId",
    "precio",
    "moneda",
    "periodicidad",
    "coberturas",
    "exclusionesResumidas",
    "vigenteHasta",
    "estado",
    "version",
    "versionReglas",
    "creadaEn",
    # HU-W06: opcionales y aditivos (el socio cotiza sin perfil: false y vacío).
    "degradada",
    "faltantes",
}


def _cotizar(app: Any, actor: dict[str, str] = ASESOR) -> Any:
    return app.cliente.post(
        "/v1/cotizaciones", json=SOLICITUD, headers={**actor, "Idempotency-Key": str(uuid4())}
    )


# --- cotizacion sobre su base ------------------------------------------------------------------


def test_ac1_la_oferta_se_guarda_con_la_cotizacion(cotizacion: Callable[..., Any]) -> None:
    app = cotizacion()
    respuesta = _cotizar(app)
    assert respuesta.status_code == 201, respuesta.text
    creada = respuesta.json()
    oferta = creada["oferta"]
    assert CODIGO.match(oferta["codigo"])
    assert (oferta["precio"], oferta["moneda"], oferta["estado"]) == ("97460", "COP", "VIGENTE")
    assert oferta["versionReglas"] == creada["versionReglas"] == "2026.10.1"
    ((codigo, cotizacion_id, precio, coberturas, exclusiones, creada_en, hasta),) = app.bases.filas(
        "SELECT codigo, cotizacion_id, precio, coberturas, exclusiones, creada_en, vigente_hasta "
        "FROM ofertas WHERE id = %s",
        oferta["id"],
    )
    assert (codigo, str(cotizacion_id), str(precio)) == (
        oferta["codigo"],
        creada["id"],
        "97460.0000",
    )
    assert {c["codigo"]: c["sumaAsegurada"] for c in coberturas} == {
        "MUERTE": "180000000",
        "INCAPACIDAD_TOTAL_PERMANENTE": "180000000",
        "ENFERMEDADES_GRAVES": "54000000",
    }
    assert len(exclusiones) == 4
    assert hasta - creada_en == timedelta(days=7)
    # La lectura devuelve la misma oferta.
    leida = app.cliente.get(f"/v1/cotizaciones/{creada['id']}", headers=ASESOR).json()
    assert leida["oferta"] == oferta


def test_los_codigos_son_unicos_y_crecientes(cotizacion: Callable[..., Any]) -> None:
    app = cotizacion()
    codigos = [_cotizar(app).json()["oferta"]["codigo"] for _ in range(3)]
    numeros = [int(c.rsplit("-", 1)[1]) for c in codigos]
    assert len(set(codigos)) == 3 and numeros == sorted(numeros)


def test_la_oferta_no_cambia_ni_se_borra(cotizacion: Callable[..., Any]) -> None:
    app = cotizacion()
    oferta = _cotizar(app).json()["oferta"]
    for sentencia in (
        "UPDATE ofertas SET precio = 1 WHERE id = %s",
        "DELETE FROM ofertas WHERE id = %s",
    ):
        with (
            psycopg.connect(app.bases.cotizacion, autocommit=True) as conexion,
            pytest.raises(psycopg.errors.RaiseException),
        ):
            conexion.execute(sentencia, (oferta["id"],))


def test_la_migracion_004_es_idempotente(cotizacion: Callable[..., Any]) -> None:
    from cotizacion.adapters.outbound.perfiles_pg import migrar

    app = cotizacion()
    antes = _cotizar(app).json()["oferta"]
    migrar(app.bases.cotizacion)
    assert (
        app.cliente.get(f"/v1/cotizaciones/{antes['cotizacionId']}", headers=ASESOR).json()[
            "oferta"
        ]
        == antes
    )


# --- El canal del socio de punta a punta ------------------------------------------------------


@dataclass
class Canal:
    """api-socios real delante de cotizacion: dos socios con permiso COTIZAR."""

    api: TestClient
    socio_a: dict[str, str]
    socio_b: dict[str, str]
    cotizacion: Any

    def cotizar(self, socio: dict[str, str]) -> Any:
        return self.api.post(
            "/cotizaciones", json=SOLICITUD, headers={**socio, "Idempotency-Key": str(uuid4())}
        )


def _base_socios(url_cotizacion: str) -> str:
    """Una base propia de api-socios en el mismo contenedor, migrada."""
    from api_socios.adapters.outbound.postgres import migrar

    base, _ = url_cotizacion.rsplit("/", 1)
    with psycopg.connect(f"{base}/postgres", autocommit=True) as conexion:
        existe = conexion.execute("SELECT 1 FROM pg_database WHERE datname = 'socios'").fetchone()
        if not existe:
            conexion.execute("CREATE DATABASE socios")
    url = f"{base}/socios"
    migrar(url)
    with psycopg.connect(url, autocommit=True) as conexion:
        conexion.execute("TRUNCATE socio")
    return url


@pytest.fixture
def canal(
    cotizacion: Callable[..., Any],
    catalogo_url: str,
    adaptador_url: str,
    servir_app: Callable[..., AbstractContextManager[str]],
) -> Iterator[Canal]:
    from api_socios.adapters.inbound.dependencias import (
        autenticar_socio,
        consultar_oferta,
        cotizar_oferta,
    )
    from api_socios.adapters.outbound.cotizacion_http import CotizacionHttp
    from api_socios.adapters.outbound.postgres import RepositorioPostgres
    from api_socios.application.ofertas import ConsultarOferta, CotizarOferta
    from api_socios.application.socios import AutenticarSocio
    from api_socios.domain.socios import CanalVenta, Permiso, Plan, Socio
    from api_socios.main import app as api_socios
    from cotizacion.config import Settings
    from cotizacion.main import crear_app

    app = cotizacion()  # siembra clientes y deja la captura de logs de la suite
    repositorio = RepositorioPostgres(_base_socios(app.bases.cotizacion))
    for numero in (1, 2):
        repositorio.guardar(
            Socio(
                id=f"SOC-00000{numero}",
                nombre=f"Socio {numero} S.A.S.",
                canal=CanalVenta.PLATAFORMA_DIGITAL,
                plan=Plan.BASICO,
                permisos=frozenset({Permiso.COTIZAR}),
                client_id=f"cliente-{numero}",
                api_key_id=f"key-{numero}",
            )
        )
    servida = crear_app(
        Settings(
            SOLVENTA_DATABASE_URL=app.bases.cotizacion,
            CATALOGO_URL=catalogo_url,
            ADAPTADOR_DATOS_URL=adaptador_url,
            CLIENTES_URL=app.clientes.url,
            dependencia_timeout_ms=1000,
            latency_budget_ms={"cotizacion": 1500},
        )
    )
    with servir_app(servida) as url:
        puerto = CotizacionHttp(url, 5000)
        api_socios.dependency_overrides[autenticar_socio] = lambda: AutenticarSocio(repositorio)
        api_socios.dependency_overrides[cotizar_oferta] = lambda: CotizarOferta(puerto)
        api_socios.dependency_overrides[consultar_oferta] = lambda: ConsultarOferta(puerto)
        try:

            def socio(numero: int) -> dict[str, str]:
                return {
                    "X-Partner-Client-Id": f"cliente-{numero}",
                    "X-Partner-Key-Id": f"key-{numero}",
                    "X-Partner-Scopes": "solventa/cotizar",
                    "X-Correlation-Id": f"corr-w04-socio-{numero}",
                }

            yield Canal(TestClient(api_socios), socio(1), socio(2), app)
        finally:
            api_socios.dependency_overrides.clear()


def test_ac1_el_socio_recibe_la_oferta_publica(canal: Canal) -> None:
    respuesta = canal.cotizar(canal.socio_a)
    assert respuesta.status_code == 201, respuesta.text
    oferta = respuesta.json()
    assert set(oferta) == CAMPOS_V1
    assert CODIGO.match(oferta["codigo"]) and oferta["estado"] == "VIGENTE"
    assert len(oferta["exclusionesResumidas"]) == 4
    # La cotización quedó a nombre del socio, que fue el actor.
    ((socio, rol, correlacion),) = canal.cotizacion.bases.filas(
        "SELECT socio_id, actor_rol, correlation_id FROM cotizaciones WHERE id = %s",
        oferta["cotizacionId"],
    )
    assert (socio, rol, correlacion) == ("SOC-000001", "SOCIO", "corr-w04-socio-1")
    leida = canal.api.get(f"/cotizaciones/{oferta['cotizacionId']}", headers=canal.socio_a)
    assert (leida.status_code, leida.json()) == (200, oferta)


def test_seguridad_un_socio_no_lee_la_oferta_de_otro(canal: Canal) -> None:
    oferta = canal.cotizar(canal.socio_a).json()
    ajena = canal.api.get(f"/cotizaciones/{oferta['cotizacionId']}", headers=canal.socio_b)
    inexistente = canal.api.get(f"/cotizaciones/{uuid4()}", headers=canal.socio_b)
    assert (ajena.status_code, ajena.json()["codigo"]) == (404, "OFERTA_NO_ENCONTRADA")
    # Igual que una que no existe: no revela que existe.
    assert {k: v for k, v in ajena.json().items() if k != "correlationId"} == {
        k: v for k, v in inexistente.json().items() if k != "correlationId"
    }


def test_un_producto_vencido_llega_al_socio_con_el_codigo_de_w03(canal: Canal) -> None:
    respuesta = canal.api.post(
        "/cotizaciones",
        json=SOLICITUD | {"productoId": "vida-hipotecario-2025", "coberturasOpcionales": []},
        headers={**canal.socio_a, "Idempotency-Key": str(uuid4())},
    )
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (
        422,
        "PRODUCTO_FUERA_DE_VIGENCIA",
    )


def _vencer(canal: Canal, oferta: dict[str, Any]) -> str:
    """Una copia de la cotización con su oferta ya vencida, sembrada por SQL (las filas son
    inmutables: no se puede adelantar la vigencia de la original)."""
    nueva, nueva_oferta = str(uuid4()), str(uuid4())
    columnas = (
        "cliente_id, socio_id, producto_id, version_producto, moneda, periodicidad, "
        "version_reglas, hash_reglas, perfil_id, nivel_riesgo, entradas, coberturas, desglose, "
        "prima_total, huella_calculo, estado, huella_solicitud, calculado_en, duracion_ms, "
        "correlation_id, actor_id, actor_rol"
    )
    with psycopg.connect(canal.cotizacion.bases.cotizacion, autocommit=True) as conexion:
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
    return nueva


def test_ac3_una_oferta_vencida_es_410_para_el_socio(canal: Canal) -> None:
    oferta = canal.cotizar(canal.socio_a).json()
    vencida = _vencer(canal, oferta)
    respuesta = canal.api.get(f"/cotizaciones/{vencida}", headers=canal.socio_a)
    assert respuesta.status_code == 410
    cuerpo = respuesta.json()
    assert cuerpo["codigo"] == "OFERTA_VENCIDA"
    assert cuerpo["detalles"]["accion"] == "NUEVA_COTIZACION"
    assert cuerpo["detalles"]["cotizacionId"] == vencida
    assert datetime.fromisoformat(cuerpo["detalles"]["vigenteHasta"]) < datetime.now().astimezone()
    # cotizacion la sigue leyendo (portal, C08): 200 con estado VENCIDA.
    interna = canal.cotizacion.cliente.get(f"/v1/cotizaciones/{vencida}", headers=ASESOR)
    assert (interna.status_code, interna.json()["oferta"]["estado"]) == (200, "VENCIDA")
    # Otro socio tampoco sabe que existe.
    ajena = canal.api.get(f"/cotizaciones/{vencida}", headers=canal.socio_b)
    assert ajena.status_code == 404
