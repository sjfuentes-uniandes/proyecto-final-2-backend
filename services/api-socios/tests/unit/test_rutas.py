from collections.abc import Callable
from io import StringIO

import pytest

ALTA = {"nombre": "Fintech Caribe S.A.S.", "canal": "plataforma_digital", "plan": "basico", "permisos": ["cotizar"]}


def alta(entorno, **cambios: object) -> dict:
    respuesta = entorno.cliente.post("/admin/socios", json=ALTA | cambios)
    assert respuesta.status_code == 201, respuesta.text
    return respuesta.json()


# --- Administración ----------------------------------------------------------------
def test_alta_devuelve_credenciales_una_sola_vez(entorno) -> None:
    respuesta = entorno.cliente.post("/admin/socios", json=ALTA | {"permisos": ["cotizar", "consultar"]})
    assert respuesta.status_code == 201
    assert respuesta.headers["cache-control"] == "no-store"
    creado = respuesta.json()
    assert creado["id"] == "SOC-000001" and creado["estado"] == "activo"
    assert creado["clientId"] and creado["clientSecret"] and creado["apiKey"]
    assert creado["permisos"] == ["consultar", "cotizar"]

    detalle = entorno.cliente.get(f"/admin/socios/{creado['id']}").text
    listado = entorno.cliente.get("/admin/socios").text
    for posterior in (detalle, listado):
        assert creado["clientId"] in posterior
        assert creado["clientSecret"] not in posterior and creado["apiKey"] not in posterior
        assert "clientSecret" not in posterior and "apiKey" not in posterior


def test_secreto_ausente_de_los_logs_del_recorrido(entorno, capturar_logs: Callable[[], StringIO]) -> None:
    salida = capturar_logs()
    creado = alta(entorno)
    entorno.cliente.get("/admin/socios")
    entorno.cliente.post(f"/admin/socios/{creado['id']}/desactivar")
    logs = salida.getvalue()
    assert creado["id"] in logs
    assert creado["clientSecret"] not in logs and creado["apiKey"] not in logs


@pytest.mark.parametrize("cuerpo", [
    ALTA | {"permisos": []},
    ALTA | {"permisos": ["administrar"]},
    ALTA | {"plan": "premium"},
    ALTA | {"nombre": "A"},
    ALTA | {"canal": "tv"},
    ALTA | {"clientSecret": "elegido-por-el-cliente"},
    {"nombre": "Sin permisos"},
])
def test_alta_invalida_responde_400(entorno, cuerpo: dict) -> None:
    respuesta = entorno.cliente.post("/admin/socios", json=cuerpo)
    assert respuesta.status_code == 400
    assert "elegido-por-el-cliente" not in respuesta.text  # no se refleja lo recibido
    assert not entorno.identidad.clientes


def test_consultar_y_desactivar_inexistente_responden_404(entorno) -> None:
    assert entorno.cliente.get("/admin/socios/SOC-999999").status_code == 404
    assert entorno.cliente.post("/admin/socios/SOC-999999/desactivar").status_code == 404
    assert entorno.cliente.get("/admin/socios/no-es-id").status_code == 400


def test_desactivar_es_idempotente_por_http(entorno) -> None:
    creado = alta(entorno)
    primera = entorno.cliente.post(f"/admin/socios/{creado['id']}/desactivar")
    segunda = entorno.cliente.post(f"/admin/socios/{creado['id']}/desactivar")
    assert primera.status_code == segunda.status_code == 200
    assert primera.json() == segunda.json() and primera.json()["estado"] == "desactivado"


@pytest.mark.parametrize("metodo, ruta", [
    ("GET", "/admin/socios"),
    ("POST", "/admin/socios"),
    ("GET", "/admin/socios/SOC-000001"),
    ("POST", "/admin/socios/SOC-000001/desactivar"),
])
@pytest.mark.parametrize("encabezado", ["X-Partner-Client-Id", "X-Partner-Key-Id"])
def test_admin_rechaza_trafico_del_api_de_socios(entorno, metodo: str, ruta: str, encabezado: str) -> None:
    creado = alta(entorno)
    respuesta = entorno.cliente.request(metodo, ruta, json=ALTA, headers={encabezado: creado["clientId"]})
    assert respuesta.status_code == 404
    assert len(entorno.repositorio.socios) == 1
    assert entorno.repositorio.socios["SOC-000001"].activo


# --- Rutas del socio ---------------------------------------------------------------
def test_socio_me_devuelve_solo_sus_datos(entorno, encabezados) -> None:
    alta(entorno, nombre="Socio A")
    alta(entorno, nombre="Socio B", permisos=["consultar"])
    a, b = entorno.repositorio.obtener("SOC-000001"), entorno.repositorio.obtener("SOC-000002")
    perfil_b = entorno.cliente.get("/socio/me", headers=encabezados(b, "consultar"))
    assert perfil_b.status_code == 200
    assert perfil_b.json()["id"] == "SOC-000002" and perfil_b.json()["nombre"] == "Socio B"
    assert a.client_id not in perfil_b.text
    assert "secret" not in perfil_b.text.lower() and "apikey" not in perfil_b.text.lower()


@pytest.mark.parametrize("caso", ["sin_encabezados", "inexistente", "api_key_de_otro", "desactivado"])
def test_credenciales_invalidas_responden_401(entorno, encabezados, caso: str) -> None:
    alta(entorno, nombre="Socio A")
    alta(entorno, nombre="Socio B")
    a, b = entorno.repositorio.obtener("SOC-000001"), entorno.repositorio.obtener("SOC-000002")
    if caso == "desactivado":
        entorno.cliente.post(f"/admin/socios/{a.id}/desactivar")
    headers = {
        "sin_encabezados": {},
        "inexistente": {"X-Partner-Client-Id": "no-existe", "X-Partner-Key-Id": a.api_key_id,
                        "X-Partner-Scopes": "solventa/cotizar"},
        "api_key_de_otro": encabezados(a, "cotizar", key_id=b.api_key_id),
        "desactivado": encabezados(a, "cotizar"),
    }[caso]
    respuesta = entorno.cliente.get("/socio/me", headers=headers)
    assert respuesta.status_code == 401
    assert respuesta.headers["www-authenticate"].startswith("Bearer")
    # AC3: el otro socio sigue autenticándose.
    assert entorno.cliente.get("/socio/me", headers=encabezados(b, "cotizar")).status_code == 200


@pytest.mark.parametrize("metodo, ruta, permiso", [
    ("POST", "/cotizaciones", "cotizar"),
    ("GET", "/cotizaciones/COT-1", "cotizar"),
    ("POST", "/polizas", "emitir"),
    ("GET", "/polizas/POL-1", "consultar"),
    ("POST", "/siniestros", "siniestros"),
])
def test_operacion_sin_permiso_responde_403(entorno, encabezados, metodo: str, ruta: str, permiso: str) -> None:
    alta(entorno, permisos=["cotizar", "emitir", "consultar", "siniestros"])
    socio = entorno.repositorio.obtener("SOC-000001")
    otros = [p for p in ("cotizar", "emitir", "consultar", "siniestros") if p != permiso]
    sin_permiso = entorno.cliente.request(metodo, ruta, headers=encabezados(socio, *otros))
    con_permiso = entorno.cliente.request(metodo, ruta, headers=encabezados(socio, permiso))
    assert sin_permiso.status_code == 403
    assert con_permiso.status_code == 501  # autorizada; la operación llega en otra historia


def test_alcance_del_token_no_amplia_los_permisos_del_socio(entorno, encabezados) -> None:
    alta(entorno, permisos=["consultar"])
    socio = entorno.repositorio.obtener("SOC-000001")
    respuesta = entorno.cliente.post("/polizas", headers=encabezados(socio, "consultar", "emitir"))
    assert respuesta.status_code == 403
