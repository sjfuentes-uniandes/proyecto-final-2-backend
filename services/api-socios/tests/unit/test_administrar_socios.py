from collections.abc import Callable
from io import StringIO

import pytest
from api_socios.application.socios import AutenticarSocio
from api_socios.domain.socios import (
    CanalVenta,
    Estado,
    NuevoSocio,
    Permiso,
    Plan,
    SocioNoEncontrado,
)


def nuevo(
    nombre: str = "Fintech Caribe S.A.S.", *permisos: Permiso, plan: Plan = Plan.BASICO
) -> NuevoSocio:
    return NuevoSocio(
        nombre=nombre,
        canal=CanalVenta.PLATAFORMA_DIGITAL,
        plan=plan,
        permisos=frozenset(permisos or {Permiso.COTIZAR}),
    )


def test_habilitar_asigna_solo_los_permisos_seleccionados(entorno) -> None:
    socio, credenciales = entorno.caso.habilitar(
        nuevo("Banco Aliado", Permiso.COTIZAR, Permiso.CONSULTAR, plan=Plan.ESTANDAR)
    )
    assert entorno.identidad.clientes[socio.client_id] == {Permiso.COTIZAR, Permiso.CONSULTAR}
    assert entorno.api_keys.planes[socio.api_key_id] is Plan.ESTANDAR
    assert (socio.estado, credenciales.client_id) == (Estado.ACTIVO, socio.client_id)
    assert credenciales.client_secret and credenciales.api_key
    assert entorno.repositorio.obtener(socio.id) == socio


def test_cada_socio_recibe_identificador_y_credenciales_propias(entorno) -> None:
    socios = [entorno.caso.habilitar(nuevo(f"Socio {n}"))[0] for n in range(3)]
    assert (
        len({s.id for s in socios})
        == len({s.client_id for s in socios})
        == len({s.api_key_id for s in socios})
        == 3
    )


def test_el_secreto_no_se_persiste_ni_se_registra(
    entorno, capturar_logs: Callable[[], StringIO]
) -> None:
    salida = capturar_logs()
    socio, credenciales = entorno.caso.habilitar(nuevo())
    guardado = repr(entorno.repositorio.socios) + repr(vars(entorno.repositorio.socios[socio.id]))
    logs = salida.getvalue()
    assert "habilitar_socio" in logs and socio.id in logs
    for valor in (credenciales.client_secret, credenciales.api_key):
        assert valor not in guardado
        assert valor not in logs


@pytest.mark.parametrize(
    "falla, clientes_eliminados, keys_eliminadas",
    [
        ("cognito", 0, 0),
        ("api_key", 1, 0),
        ("plan", 1, 1),
        ("base", 1, 1),
    ],
)
def test_compensa_los_pasos_anteriores_si_uno_falla(
    entorno, falla: str, clientes_eliminados: int, keys_eliminadas: int
) -> None:
    entorno.identidad.falla = falla == "cognito"
    entorno.api_keys.falla_crear = falla == "api_key"
    entorno.api_keys.falla_plan = falla == "plan"
    entorno.repositorio.falla_al_guardar = falla == "base"
    with pytest.raises(RuntimeError):
        entorno.caso.habilitar(nuevo())
    assert len(entorno.identidad.eliminados) == clientes_eliminados
    assert len(entorno.api_keys.eliminadas) == keys_eliminadas
    assert not entorno.identidad.clientes and not entorno.api_keys.keys
    assert not entorno.repositorio.socios


def test_compensacion_no_oculta_el_error_original(entorno, monkeypatch: pytest.MonkeyPatch) -> None:
    entorno.api_keys.falla_plan = True

    def eliminar_falla(_: str) -> None:
        raise ConnectionError("sin red")

    monkeypatch.setattr(entorno.identidad, "eliminar_cliente", eliminar_falla)
    with pytest.raises(RuntimeError, match="plan inexistente"):
        entorno.caso.habilitar(nuevo())
    assert entorno.api_keys.eliminadas  # la API key sí se compensó


def test_desactivar_afecta_solo_a_ese_socio(entorno) -> None:
    a, _ = entorno.caso.habilitar(nuevo("Socio A"))
    b, _ = entorno.caso.habilitar(nuevo("Socio B"))
    desactivado = entorno.caso.desactivar(a.id)
    assert desactivado.estado is Estado.DESACTIVADO and desactivado.desactivado is not None
    assert entorno.identidad.eliminados == [a.client_id]
    assert entorno.api_keys.keys == {a.api_key_id: False, b.api_key_id: True}
    assert entorno.repositorio.obtener(b.id).estado is Estado.ACTIVO
    autenticar = AutenticarSocio(entorno.repositorio)
    assert autenticar.ejecutar(a.client_id, a.api_key_id) is None
    assert autenticar.ejecutar(b.client_id, b.api_key_id) == b


def test_desactivar_es_idempotente(entorno) -> None:
    a, _ = entorno.caso.habilitar(nuevo())
    primera = entorno.caso.desactivar(a.id)
    assert entorno.caso.desactivar(a.id) == primera
    assert entorno.identidad.eliminados == [a.client_id]


def test_desactivar_socio_inexistente(entorno) -> None:
    with pytest.raises(SocioNoEncontrado):
        entorno.caso.desactivar("SOC-999999")


def test_autenticacion_rechazada_se_registra_sin_secreto(
    entorno, capturar_logs: Callable[[], StringIO]
) -> None:
    a, _ = entorno.caso.habilitar(nuevo())
    salida = capturar_logs()
    autenticar = AutenticarSocio(entorno.repositorio)
    assert autenticar.ejecutar("cliente-inexistente", "key1") is None
    assert autenticar.ejecutar(a.client_id, "key-ajena") is None
    assert autenticar.ejecutar(None, None) is None
    logs = salida.getvalue()
    assert logs.count("credencial de socio rechazada") == 3
    assert '"motivo": "inexistente"' in logs and '"motivo": "api_key_ajena"' in logs
    assert '"result": "rechazado"' in logs
