import pytest
from api_socios.domain.socios import Plan

ALTA = {
    "nombre": "Retail Andino",
    "canal": "comercio",
    "plan": "basico",
    "permisos": ["cotizar"],
}


def alta(entorno, **cambios: object) -> dict:
    respuesta = entorno.cliente.post("/admin/socios", json=ALTA | cambios)
    assert respuesta.status_code == 201, respuesta.text
    return respuesta.json()


def test_cambiar_plan(entorno) -> None:
    creado = alta(entorno)
    respuesta = entorno.cliente.put(f"/admin/socios/{creado['id']}/plan", json={"plan": "prueba"})
    assert respuesta.status_code == 200
    assert respuesta.json()["plan"] == "prueba" and respuesta.json()["id"] == creado["id"]
    assert "apiKey" not in respuesta.text and "clientSecret" not in respuesta.text
    assert entorno.cliente.get(f"/admin/socios/{creado['id']}").json()["plan"] == "prueba"


@pytest.mark.parametrize(
    "cuerpo", [{"plan": "oro"}, {}, {"plan": "prueba", "apiKeyId": "key-de-otro"}]
)
def test_cambio_de_plan_invalido(entorno, cuerpo: dict) -> None:
    creado = alta(entorno)
    respuesta = entorno.cliente.put(f"/admin/socios/{creado['id']}/plan", json=cuerpo)
    assert respuesta.status_code == 422 and respuesta.json()["codigo"] == "SOLICITUD_INVALIDA"
    assert entorno.repositorio.obtener(creado["id"]).plan is Plan.BASICO


def test_plan_no_disponible_en_el_ambiente(entorno) -> None:
    creado = alta(entorno)
    entorno.api_keys.disponibles = [Plan.BASICO, Plan.ESTANDAR]
    respuesta = entorno.cliente.put(f"/admin/socios/{creado['id']}/plan", json={"plan": "prueba"})
    assert respuesta.status_code == 422
    assert respuesta.json()["codigo"] == "PLAN_NO_DISPONIBLE"
    assert respuesta.json()["detalles"] == {"plan": "prueba"}
    alta_prueba = entorno.cliente.post("/admin/socios", json=ALTA | {"plan": "prueba"})
    assert alta_prueba.status_code == 422 and alta_prueba.json()["codigo"] == "PLAN_NO_DISPONIBLE"
    assert len(entorno.repositorio.socios) == 1


def test_cambio_de_plan_de_socio_desactivado_o_inexistente(entorno) -> None:
    creado = alta(entorno)
    entorno.cliente.post(f"/admin/socios/{creado['id']}/desactivar")
    desactivado = entorno.cliente.put(
        f"/admin/socios/{creado['id']}/plan", json={"plan": "estandar"}
    )
    assert desactivado.status_code == 409 and desactivado.json()["codigo"] == "SOCIO_DESACTIVADO"
    inexistente = entorno.cliente.put("/admin/socios/SOC-999999/plan", json={"plan": "estandar"})
    assert inexistente.status_code == 404
    assert inexistente.json()["codigo"] == "SOCIO_NO_ENCONTRADO"
    assert entorno.cliente.get("/admin/socios/SOC-999999/consumo").status_code == 404


def test_consumo_del_socio(entorno) -> None:
    creado = alta(entorno, plan="prueba")
    socio = entorno.repositorio.obtener(creado["id"])
    entorno.api_keys.usado[socio.api_key_id] = 4
    respuesta = entorno.cliente.get(f"/admin/socios/{creado['id']}/consumo")
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["socioId"] == creado["id"]
    assert cuerpo["limites"] == {
        "plan": "prueba",
        "limitePorSegundo": 1.0,
        "rafaga": 2,
        "cuota": 5,
        "periodo": "DAY",
    }
    assert (cuerpo["usado"], cuerpo["restante"], cuerpo["porcentaje"], cuerpo["estado"]) == (
        4,
        1,
        80,
        "cerca_del_limite",
    )
    assert cuerpo["ventana"]["inicio"] == cuerpo["ventana"]["fin"]
    assert cuerpo["ventana"]["reinicio"].endswith("T00:00:00Z")
    assert cuerpo["limitadas"] is None
    assert socio.api_key_id not in respuesta.text


def test_el_consumo_no_incluye_datos_de_otro_socio(entorno) -> None:
    a = alta(entorno, nombre="Socio A", plan="prueba")
    b = alta(entorno, nombre="Socio B", plan="prueba")
    entorno.api_keys.usado[entorno.repositorio.obtener(a["id"]).api_key_id] = 5
    consumo_b = entorno.cliente.get(f"/admin/socios/{b['id']}/consumo")
    assert consumo_b.json()["estado"] == "normal" and consumo_b.json()["usado"] == 0
    for dato in (a["id"], a["clientId"], a["nombre"]):
        assert dato not in consumo_b.text


def test_planes_disponibles(entorno) -> None:
    entorno.api_keys.disponibles = [Plan.BASICO, Plan.ESTANDAR]
    respuesta = entorno.cliente.get("/admin/socios/planes")
    assert respuesta.status_code == 200
    assert [(p["plan"], p["cuota"], p["periodo"]) for p in respuesta.json()] == [
        ("basico", 1000, "DAY"),
        ("estandar", 50000, "DAY"),
    ]


@pytest.mark.parametrize(
    "metodo, ruta, cuerpo",
    [
        ("PUT", "/admin/socios/SOC-000001/plan", {"plan": "estandar"}),
        ("GET", "/admin/socios/SOC-000001/consumo", None),
        ("GET", "/admin/socios/SOC-000002/consumo", None),
        ("GET", "/admin/socios/planes", None),
    ],
)
def test_un_socio_no_puede_ampliar_su_cuota_ni_ver_el_consumo_ajeno(
    entorno, encabezados, metodo: str, ruta: str, cuerpo: dict | None
) -> None:
    alta(entorno, nombre="Socio A")
    alta(entorno, nombre="Socio B")
    a = entorno.repositorio.obtener("SOC-000001")
    respuesta = entorno.cliente.request(
        metodo, ruta, json=cuerpo, headers=encabezados(a, "cotizar", "consultar")
    )
    assert respuesta.status_code == 404 and respuesta.json()["codigo"] == "NO_ENCONTRADO"
    assert entorno.repositorio.obtener("SOC-000001").plan is Plan.BASICO
    assert entorno.api_keys.planes[a.api_key_id] is Plan.BASICO
    assert not entorno.api_keys.consultas_uso
