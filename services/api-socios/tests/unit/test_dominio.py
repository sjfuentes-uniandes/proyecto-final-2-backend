from datetime import UTC, datetime

import pytest
from api_socios.adapters.inbound.admin import SocioSolicitud
from api_socios.domain.operaciones import PERMISO_POR_OPERACION, permiso_requerido
from api_socios.domain.socios import (
    CanalVenta,
    CredencialesEmitidas,
    Estado,
    NuevoSocio,
    Permiso,
    Plan,
    Socio,
    es_identificador,
    identificador,
    permisos_de_alcances,
)
from api_socios.main import app
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError


def socio(**cambios: object) -> Socio:
    datos: dict[str, object] = {
        "id": "SOC-000001",
        "nombre": "Fintech Caribe S.A.S.",
        "canal": CanalVenta.PLATAFORMA_DIGITAL,
        "plan": Plan.BASICO,
        "permisos": frozenset({Permiso.COTIZAR}),
        "client_id": "cliente1",
        "api_key_id": "key1",
    }
    return Socio(**(datos | cambios))  # type: ignore[arg-type]


def test_identificador_con_formato_del_mockup() -> None:
    assert identificador(161) == "SOC-000161"
    assert (
        es_identificador("SOC-000161")
        and not es_identificador("SOC-161")
        and not es_identificador("soc-000161")
    )
    assert len({identificador(n) for n in range(1, 1001)}) == 1000
    for fuera in (0, 1_000_000):
        with pytest.raises(ValueError):
            identificador(fuera)


@pytest.mark.parametrize(
    "nombre, permisos",
    [
        ("A", {Permiso.COTIZAR}),
        ("x" * 121, {Permiso.COTIZAR}),
        ("   ", {Permiso.COTIZAR}),
        ("\x1e\x17 ", {Permiso.COTIZAR}),
        ("Fintech\x00Caribe", {Permiso.COTIZAR}),
        ("Fintech Caribe", set()),
    ],
)
def test_nuevo_socio_invalido(nombre: str, permisos: set[Permiso]) -> None:
    with pytest.raises(ValueError):
        NuevoSocio(
            nombre=nombre, canal=CanalVenta.COMERCIO, plan=Plan.BASICO, permisos=frozenset(permisos)
        )


def test_nuevo_socio_normaliza_nombre() -> None:
    nuevo = NuevoSocio(
        nombre="  Retail Andino ",
        canal=CanalVenta.COMERCIO,
        plan=Plan.ESTANDAR,
        permisos=frozenset({Permiso.CONSULTAR}),
    )
    assert nuevo.nombre == "Retail Andino"


def test_credenciales_no_exponen_el_secreto_en_repr() -> None:
    credenciales = CredencialesEmitidas(
        client_id="abc", client_secret="s3cr3t-valor", api_key="k3y-valor"
    )
    texto = repr(credenciales) + str(credenciales)
    assert "abc" in texto and "s3cr3t-valor" not in texto and "k3y-valor" not in texto


def test_el_socio_no_tiene_donde_guardar_el_secreto() -> None:
    campos = set(Socio.__dataclass_fields__)
    assert not {c for c in campos if "secret" in c or c == "api_key"}


def test_desactivar_es_idempotente_y_conserva_la_fecha() -> None:
    activo = socio()
    primera = activo.desactivar(datetime(2026, 10, 1, tzinfo=UTC))
    segunda = primera.desactivar(datetime(2026, 10, 2, tzinfo=UTC))
    assert activo.estado is Estado.ACTIVO  # inmutable
    assert primera.estado is Estado.DESACTIVADO and segunda is primera
    assert segunda.desactivado == datetime(2026, 10, 1, tzinfo=UTC)


def test_autentica_solo_activo_con_su_propia_api_key() -> None:
    activo = socio()
    assert activo.autentica("cliente1", "key1")
    assert not activo.autentica("cliente1", "key-de-otro")
    assert not activo.autentica("cliente1", None)
    assert not activo.autentica("otro", "key1")
    assert not activo.desactivar().autentica("cliente1", "key1")


def test_permisos_de_alcances_ignora_otros_servidores_y_desconocidos() -> None:
    alcances = "solventa/cotizar solventa/emitir otro/consultar solventa/admin aws.cognito.signin.user.admin"
    assert permisos_de_alcances(alcances, "solventa") == {Permiso.COTIZAR, Permiso.EMITIR}
    assert permisos_de_alcances(None, "solventa") == frozenset()


def test_cada_operacion_de_socio_tiene_permiso() -> None:
    rutas = {
        (metodo.upper(), ruta)
        for ruta, operaciones in app.openapi()["paths"].items()
        for metodo, operacion in operaciones.items()
        if operacion.get("tags") == ["socio"] and ruta != "/socio/me"
    }
    assert rutas == set(PERMISO_POR_OPERACION)
    assert permiso_requerido("get", "/polizas/{poliza_id}") is Permiso.CONSULTAR
    with pytest.raises(LookupError):
        permiso_requerido("DELETE", "/polizas/{poliza_id}")


@given(st.text(max_size=130))
def test_el_contrato_y_el_dominio_aceptan_la_misma_razon_social(nombre: str) -> None:
    datos = {"nombre": nombre, "canal": "otro", "plan": "basico", "permisos": ["cotizar"]}
    try:
        SocioSolicitud.model_validate(datos)
        contrato = True
    except ValidationError:
        contrato = False
    try:
        NuevoSocio(nombre, CanalVenta.OTRO, Plan.BASICO, frozenset({Permiso.COTIZAR}))
        dominio = True
    except ValueError:
        dominio = False
    assert contrato == dominio
