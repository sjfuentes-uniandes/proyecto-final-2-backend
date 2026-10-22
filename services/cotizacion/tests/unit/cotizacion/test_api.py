"""API interna de la cotización (HU-W03) sobre la app real con los puertos en fakes: actor,
autorización por socio, códigos estables, idempotencia y condiciones del producto."""

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from cotizacion.config import Settings
from cotizacion.domain.cotizacion.modelos import EstadoProducto
from cotizacion.domain.perfilamiento.errores import ClienteNoEncontrado, DependenciaNoDisponible
from cotizacion.main import Puertos, crear_app
from fastapi.testclient import TestClient

ASESOR = {"X-Actor-Id": "asesor-sintetico", "X-Actor-Rol": "ASESOR"}
SOCIO_1 = {"X-Actor-Id": "SOC-000001", "X-Actor-Rol": "SOCIO"}
SOCIO_2 = {"X-Actor-Id": "SOC-000002", "X-Actor-Rol": "SOCIO"}
C01 = {
    "clienteId": "00000000-0000-4000-8000-000000000001",
    "productoId": "vida-hipotecario",
    "datosAsegurado": {
        "fechaNacimiento": "1990-04-18",
        "plazoCreditoMeses": 180,
        "sumaAsegurada": 180000000,
    },
}


def clave(n: int = 1) -> dict[str, str]:
    return {"Idempotency-Key": f"00000000-0000-4000-c000-{n:012d}"}


@pytest.fixture
def api(escenario: Any, escenario_cotizacion: Any) -> Iterator[TestClient]:
    puertos = Puertos(
        identidad=escenario_cotizacion.identidad,
        consentimiento=escenario.consentimiento,
        modelos=escenario.modelos,
        ejecutor=escenario.ejecutor,
        perfiles=escenario_cotizacion.perfiles,
        reloj=escenario_cotizacion.reloj,
        productos=escenario_cotizacion.productos,
        cotizaciones=escenario_cotizacion.repo,
    )
    with TestClient(crear_app(Settings(), puertos=puertos)) as cliente:
        yield cliente


def cotizar(api: TestClient, cuerpo: dict[str, Any] | None = None, **encabezados: str) -> Any:
    return api.post(
        "/v1/cotizaciones", json=cuerpo or C01, headers={**ASESOR, **clave(), **encabezados}
    )


def test_201_con_prima_en_texto_y_sin_hash_de_reglas(api: TestClient) -> None:
    respuesta = cotizar(api)
    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    assert (cuerpo["primaTotal"], cuerpo["moneda"]) == ("82593", "COP")
    assert cuerpo["desglose"]["primaTotal"] == "82593"
    assert cuerpo["versionReglas"] == "2026.09.1"
    assert cuerpo["entradas"] == {
        "sumaAsegurada": "180000000",
        "plazoCreditoMeses": 180,
        "edad": 36,
        "nivelRiesgo": None,
        "coberturasOpcionales": [],
    }
    assert cuerpo["presupuestoMs"] == 250
    assert next(p["codigo"] for p in cuerpo["pasos"]) == "CATALOGO_Y_REGLAS"
    assert "hashReglas" not in cuerpo and "fechaNacimiento" not in str(cuerpo)


def test_misma_clave_responde_200_y_la_lectura_es_igual(api: TestClient) -> None:
    primera = cotizar(api).json()
    repetida = cotizar(api)
    assert repetida.status_code == 200
    assert repetida.json()["id"] == primera["id"] and repetida.json()["pasos"] == []
    leida = api.get(f"/v1/cotizaciones/{primera['id']}", headers=ASESOR).json()
    assert {k: v for k, v in leida.items() if k != "pasos"} == {
        k: v for k, v in primera.items() if k != "pasos"
    }


@pytest.mark.parametrize(
    ("encabezados", "estado", "codigo"),
    [
        ({}, 401, "ACTOR_REQUERIDO"),
        ({"X-Actor-Id": "x", "X-Actor-Rol": "CLIENTE"}, 403, "ROL_NO_AUTORIZADO"),
        ({"X-Actor-Id": "socio-sin-formato", "X-Actor-Rol": "SOCIO"}, 403, "ROL_NO_AUTORIZADO"),
    ],
)
def test_actor(api: TestClient, encabezados: dict[str, str], estado: int, codigo: str) -> None:
    respuesta = api.post("/v1/cotizaciones", json=C01, headers={**encabezados, **clave()})
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (estado, codigo)


def test_un_socio_solo_lee_sus_cotizaciones(api: TestClient) -> None:
    propia = api.post("/v1/cotizaciones", json=C01, headers={**SOCIO_1, **clave()}).json()
    assert propia["socioId"] == "SOC-000001"
    url = f"/v1/cotizaciones/{propia['id']}"
    assert api.get(url, headers=SOCIO_1).status_code == 200
    ajena = api.get(url, headers=SOCIO_2)
    assert (ajena.status_code, ajena.json()["codigo"]) == (404, "COTIZACION_NO_ENCONTRADA")
    assert api.get(url, headers=ASESOR).status_code == 200
    asesor = cotizar(api, **clave(2)).json()
    otra = api.get(f"/v1/cotizaciones/{asesor['id']}", headers=SOCIO_1)
    assert otra.status_code == 404


def test_un_socio_no_usa_perfiles(api: TestClient) -> None:
    cuerpo = {**C01, "perfilId": "00000000-0000-4000-a000-000000000001"}
    respuesta = api.post("/v1/cotizaciones", json=cuerpo, headers={**SOCIO_1, **clave()})
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (403, "ROL_NO_AUTORIZADO")


def test_cotizacion_inexistente(api: TestClient) -> None:
    respuesta = api.get(f"/v1/cotizaciones/{UUID(int=0)}", headers=ASESOR)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (404, "COTIZACION_NO_ENCONTRADA")


@pytest.mark.parametrize(
    ("preparar", "cuerpo", "estado", "codigo"),
    [
        (None, {**C01, "productoId": "no-existe"}, 422, "PRODUCTO_NO_ENCONTRADO"),
        ("inactivo", C01, 422, "PRODUCTO_INACTIVO"),
        (None, {**C01, "productoId": "vida-hipotecario-2025"}, 422, "PRODUCTO_FUERA_DE_VIGENCIA"),
        ("sin reglas", C01, 422, "REGLAS_NO_VIGENTES"),
        (
            None,
            {**C01, "datosAsegurado": {**C01["datosAsegurado"], "sumaAsegurada": 0}},
            422,
            "DATOS_FUERA_DE_RANGO",
        ),
        (None, {**C01, "coberturasOpcionales": ["MUERTE"]}, 422, "COBERTURA_NO_DISPONIBLE"),
        (
            None,
            {**C01, "perfilId": "00000000-0000-4000-a000-0000000000ff"},
            404,
            "PERFIL_NO_ENCONTRADO",
        ),
        (
            None,
            {
                **C01,
                "perfilId": "00000000-0000-4000-a000-000000000001",
                "datosAsegurado": {**C01["datosAsegurado"], "plazoCreditoMeses": 240},
            },
            422,
            "PERFIL_NO_CORRESPONDE",
        ),
        ("cliente", C01, 404, "CLIENTE_NO_ENCONTRADO"),
        ("catalogo caido", C01, 503, "DEPENDENCIA_NO_DISPONIBLE"),
        (None, {**C01, "socioId": "socio"}, 422, "SOLICITUD_INVALIDA"),
    ],
)
def test_codigos_estables(
    api: TestClient,
    escenario_cotizacion: Any,
    preparar: str | None,
    cuerpo: dict[str, Any],
    estado: int,
    codigo: str,
) -> None:
    from dataclasses import replace

    productos = escenario_cotizacion.productos
    if preparar == "inactivo":
        productos.productos["vida-hipotecario"] = replace(
            productos.productos["vida-hipotecario"], estado=EstadoProducto.INACTIVO
        )
    elif preparar == "sin reglas":
        productos.reglas.pop("vida-hipotecario")
    elif preparar == "cliente":
        escenario_cotizacion.identidad.respuesta = ClienteNoEncontrado()
    elif preparar == "catalogo caido":
        productos.errores_producto["vida-hipotecario"] = DependenciaNoDisponible("catalogo")
    respuesta = cotizar(api, cuerpo)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (estado, codigo)
    detalles = respuesta.json()["detalles"]
    assert "180000000" not in str(detalles) and "1990-04-18" not in str(detalles)


def test_campos_fuera_de_rango_sin_valores(api: TestClient) -> None:
    cuerpo = {**C01, "datosAsegurado": {**C01["datosAsegurado"], "sumaAsegurada": 1400000000}}
    respuesta = cotizar(api, cuerpo)
    assert respuesta.json()["detalles"] == {"campos": ["datosAsegurado.sumaAsegurada"]}


def test_conflicto_de_idempotencia(api: TestClient) -> None:
    cotizar(api)
    otra = {**C01, "coberturasOpcionales": ["AUXILIO_FUNERARIO"]}
    respuesta = cotizar(api, otra)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (422, "IDEMPOTENCIA_CONFLICTO")


def test_falta_la_clave_de_idempotencia(api: TestClient) -> None:
    respuesta = api.post("/v1/cotizaciones", json=C01, headers=ASESOR)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (422, "SOLICITUD_INVALIDA")


def test_condiciones_del_producto(api: TestClient) -> None:
    respuesta = api.get("/v1/productos/vida-hipotecario/condiciones", headers=SOCIO_1)
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert (cuerpo["versionProducto"], cuerpo["versionReglas"]) == ("2026.09.1", "2026.09.1")
    assert cuerpo["limites"]["sumaAsegurada"] == {"minimo": "10000000", "maximo": "1000000000"}
    assert cuerpo["limites"]["edad"] == {"minimo": "18", "maximo": "70"}
    assert [c["obligatoria"] for c in cuerpo["coberturas"]] == [True, True, False, False]


@pytest.mark.parametrize(
    ("producto", "preparar", "estado", "codigo"),
    [
        ("no-existe", None, 404, "PRODUCTO_NO_ENCONTRADO"),
        ("vida-hipotecario-2025", None, 422, "PRODUCTO_FUERA_DE_VIGENCIA"),
        ("vida-hipotecario", "inactivo sin reglas", 422, "PRODUCTO_INACTIVO"),
        ("vida-hipotecario", "sin reglas", 422, "REGLAS_NO_VIGENTES"),
        ("vida-hipotecario", "caido", 503, "DEPENDENCIA_NO_DISPONIBLE"),
    ],
)
def test_condiciones_rechazadas(
    api: TestClient,
    escenario_cotizacion: Any,
    producto: str,
    preparar: str | None,
    estado: int,
    codigo: str,
) -> None:
    from dataclasses import replace

    productos = escenario_cotizacion.productos
    if preparar in ("sin reglas", "inactivo sin reglas"):
        productos.reglas.pop("vida-hipotecario")
    if preparar == "inactivo sin reglas":
        productos.productos["vida-hipotecario"] = replace(
            productos.productos["vida-hipotecario"], estado=EstadoProducto.INACTIVO
        )
    if preparar == "caido":
        productos.errores_reglas["vida-hipotecario"] = DependenciaNoDisponible("catalogo")
    respuesta = api.get(f"/v1/productos/{producto}/condiciones", headers=ASESOR)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (estado, codigo)


def test_sin_puertos_de_cotizacion_responde_503(escenario: Any) -> None:
    """Quien arma la app sin los puertos de HU-W03 (pruebas de W10–W31) recibe 503, no 500."""
    puertos = Puertos(
        identidad=escenario.identidad,
        consentimiento=escenario.consentimiento,
        modelos=escenario.modelos,
        ejecutor=escenario.ejecutor,
        perfiles=escenario.repo,
        reloj=escenario.reloj,
    )
    with TestClient(crear_app(Settings(), puertos=puertos)) as api:
        respuesta = cotizar(api)
        lectura = api.get(f"/v1/cotizaciones/{UUID(int=0)}", headers=ASESOR)
        condiciones = api.get("/v1/productos/vida-hipotecario/condiciones", headers=ASESOR)
    for r in (respuesta, lectura, condiciones):
        assert (r.status_code, r.json()["codigo"]) == (503, "DEPENDENCIA_NO_DISPONIBLE")
