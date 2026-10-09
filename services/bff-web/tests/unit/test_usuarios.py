from datetime import UTC, datetime
from typing import Any

import pytest
from bff_web.adapters.inbound import usuarios as ruta
from bff_web.adapters.outbound.cognito import DirectorioCognito
from bff_web.application.usuarios import AdministrarUsuarios
from bff_web.domain.usuarios import NuevoUsuario, Usuario, UsuarioYaExiste
from bff_web.main import app
from fastapi.testclient import TestClient

ADMIN = {"X-Authenticated-Groups": "[administradores, operacion]"}
CREADO = datetime(2026, 10, 9, tzinfo=UTC)


# --- Dominio -------------------------------------------------------------------------
@pytest.mark.parametrize("datos", [
    {"email": "no-es-correo", "nombre": "Ana", "grupos": ("operacion",)},
    {"email": "ana@solventa.co", "nombre": "A", "grupos": ("operacion",)},
    {"email": "ana@solventa.co", "nombre": "Ana", "grupos": ()},
    {"email": "ana@solventa.co", "nombre": "Ana", "grupos": ("superusuario",)},
])
def test_nuevo_usuario_invalido(datos: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        NuevoUsuario(**datos)


# --- Adaptador Cognito -------------------------------------------------------------------
class CognitoFalso:
    class exceptions:
        class UsernameExistsException(Exception):
            pass

    def __init__(self) -> None:
        self.usuarios = [
            {"Username": "u1", "Attributes": [{"Name": "email", "Value": "ana@solventa.co"}, {"Name": "name", "Value": "Ana"}],
             "UserStatus": "CONFIRMED", "Enabled": True, "UserCreateDate": CREADO},
            {"Username": "u2", "Attributes": [{"Name": "email", "Value": "luis@solventa.co"}],
             "UserStatus": "FORCE_CHANGE_PASSWORD", "Enabled": True, "UserCreateDate": CREADO},
        ]
        self.grupos = {"administradores": ["u1"], "operacion": ["u1", "u2"]}
        self.llamadas: list[tuple[str, dict[str, Any]]] = []

    def get_paginator(self, operacion: str) -> Any:
        cognito = self

        class Paginador:
            def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
                cognito.llamadas.append((operacion, kwargs))
                if operacion == "list_users":
                    return [{"Users": cognito.usuarios[:1]}, {"Users": cognito.usuarios[1:]}]
                ids = cognito.grupos.get(kwargs["GroupName"], [])
                return [{"Users": [u for u in cognito.usuarios if u["Username"] in ids]}]

        return Paginador()

    def admin_create_user(self, **kwargs: Any) -> dict[str, Any]:
        self.llamadas.append(("admin_create_user", kwargs))
        if kwargs["Username"] == "ana@solventa.co":
            raise self.exceptions.UsernameExistsException()
        return {"User": {"Username": "u3", "Attributes": kwargs["UserAttributes"], "UserStatus": "FORCE_CHANGE_PASSWORD",
                         "Enabled": True, "UserCreateDate": CREADO}}

    def admin_add_user_to_group(self, **kwargs: Any) -> None:
        self.llamadas.append(("admin_add_user_to_group", kwargs))


def test_lista_usuarios_con_sus_grupos() -> None:
    cognito = CognitoFalso()
    usuarios = DirectorioCognito(cognito, "us-east-1_b").listar()
    assert [(u.email, u.nombre, u.estado, u.grupos) for u in usuarios] == [
        ("ana@solventa.co", "Ana", "CONFIRMED", ["administradores", "operacion"]),
        ("luis@solventa.co", None, "FORCE_CHANGE_PASSWORD", ["operacion"]),
    ]
    assert all(kwargs["UserPoolId"] == "us-east-1_b" for _, kwargs in cognito.llamadas)


def test_crea_usuario_con_invitacion_por_correo_y_grupos() -> None:
    cognito = CognitoFalso()
    usuario = DirectorioCognito(cognito, "us-east-1_b").crear(
        NuevoUsuario("nuevo@solventa.co", " Nuevo Usuario ", ("operacion", "administradores-socios"))
    )
    assert (usuario.email, usuario.nombre, usuario.estado) == ("nuevo@solventa.co", "Nuevo Usuario", "FORCE_CHANGE_PASSWORD")
    crear = next(kwargs for op, kwargs in cognito.llamadas if op == "admin_create_user")
    assert crear["DesiredDeliveryMediums"] == ["EMAIL"]
    assert "TemporaryPassword" not in crear  # la genera Cognito y solo la recibe el usuario
    assert {"Name": "email_verified", "Value": "true"} in crear["UserAttributes"]
    grupos = [kwargs["GroupName"] for op, kwargs in cognito.llamadas if op == "admin_add_user_to_group"]
    assert grupos == ["operacion", "administradores-socios"]


def test_usuario_existente() -> None:
    with pytest.raises(UsuarioYaExiste):
        DirectorioCognito(CognitoFalso(), "p").crear(NuevoUsuario("ana@solventa.co", "Ana", ("operacion",)))


# --- Rutas ---------------------------------------------------------------------------------
class DirectorioEnMemoria:
    def __init__(self) -> None:
        self.creados: list[NuevoUsuario] = []

    def listar(self) -> list[Usuario]:
        return [Usuario("b@solventa.co", "B", "CONFIRMED", True, CREADO, ["operacion"]),
                Usuario("a@solventa.co", None, "FORCE_CHANGE_PASSWORD", True, CREADO, [])]

    def crear(self, usuario: NuevoUsuario) -> Usuario:
        if usuario.email == "a@solventa.co":
            raise UsuarioYaExiste(usuario.email)
        self.creados.append(usuario)
        return Usuario(usuario.email, usuario.nombre, "FORCE_CHANGE_PASSWORD", True, CREADO, list(usuario.grupos))


@pytest.fixture
def http() -> Any:
    directorio = DirectorioEnMemoria()
    app.dependency_overrides[ruta.administrar_usuarios] = lambda: AdministrarUsuarios(directorio)
    yield TestClient(app), directorio
    app.dependency_overrides.clear()


def test_listar_ordenado(http: Any) -> None:
    cliente, _ = http
    respuesta = cliente.get("/backoffice/usuarios", headers=ADMIN)
    assert respuesta.status_code == 200
    assert [u["email"] for u in respuesta.json()] == ["a@solventa.co", "b@solventa.co"]


def test_crear_usuario(http: Any) -> None:
    cliente, directorio = http
    respuesta = cliente.post("/backoffice/usuarios", headers=ADMIN, json={
        "email": " Nuevo@Solventa.co ", "nombre": "Nuevo Usuario", "grupos": ["operacion", "operacion"],
    })
    assert respuesta.status_code == 201
    assert respuesta.json()["estado"] == "FORCE_CHANGE_PASSWORD"
    assert directorio.creados == [NuevoUsuario("nuevo@solventa.co", "Nuevo Usuario", ("operacion",))]


@pytest.mark.parametrize(("cuerpo", "codigo"), [
    ({"email": "a@solventa.co", "nombre": "Ana", "grupos": ["operacion"]}, 409),
    ({"email": "x@solventa.co", "nombre": "X", "grupos": ["operacion"]}, 422),
    ({"email": "x@solventa.co", "nombre": "Xavier", "grupos": ["root"]}, 422),
    ({"email": "x@solventa.co", "nombre": "Xavier", "grupos": []}, 422),
    ({"email": "sin-arroba", "nombre": "Xavier", "grupos": ["operacion"]}, 422),
])
def test_crear_rechazos(http: Any, cuerpo: dict[str, Any], codigo: int) -> None:
    cliente, _ = http
    assert cliente.post("/backoffice/usuarios", headers=ADMIN, json=cuerpo).status_code == codigo


@pytest.mark.parametrize("grupos", [None, "[operacion]", "[administradores-socios]"])
def test_solo_administradores(http: Any, grupos: str | None) -> None:
    cliente, directorio = http
    encabezados = {"X-Authenticated-Groups": grupos} if grupos else {}
    assert cliente.get("/backoffice/usuarios", headers=encabezados).status_code == 403
    assert cliente.post("/backoffice/usuarios", headers=encabezados,
                        json={"email": "x@solventa.co", "nombre": "Xavier", "grupos": ["administradores"]}).status_code == 403
    assert directorio.creados == []


def test_grupos_disponibles(http: Any) -> None:
    cliente, _ = http
    nombres = [g["nombre"] for g in cliente.get("/backoffice/grupos", headers=ADMIN).json()]
    assert nombres == ["administradores", "administradores-socios", "operacion"]


def test_sin_pool_configurado_503() -> None:
    ruta.administrar_usuarios.cache_clear()
    assert TestClient(app).get("/backoffice/usuarios", headers=ADMIN).status_code == 503
