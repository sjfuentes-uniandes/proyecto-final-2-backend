"""API de dispositivos: 401/403, registro, baja y que el token nunca viaje en la URL ni en la
respuesta. Usa casos de uso falsos (patrón de `services/consulta/tests` para `polizas.py`): el
registro/reasignación reales se prueban con `DispositivosPg` (conexión falsa) y con Postgres real
(`tests/module`).
"""

from typing import Any
from uuid import UUID, uuid4

from avisos.adapters.inbound.http import dispositivos
from avisos.application.dispositivos.puertos import DispositivoNoAutorizado, DispositivoNoEncontrado
from fastapi import FastAPI
from fastapi.testclient import TestClient
from solventa_common.errors import registrar_manejadores

CLIENTE = uuid4()
DISPOSITIVO = uuid4()
TOKEN = "token-sintetico-entregado"
CABECERAS = {"X-Actor-Id": str(CLIENTE), "X-Actor-Rol": "CLIENTE"}


class _RegistrarFalso:
    def __init__(self, dispositivo_id: UUID = DISPOSITIVO) -> None:
        self.dispositivo_id = dispositivo_id
        self.llamadas: list[tuple[UUID, str]] = []

    async def ejecutar(self, cliente_id: UUID, token: str) -> UUID:
        self.llamadas.append((cliente_id, token))
        return self.dispositivo_id


class _DarDeBajaFalso:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.llamadas: list[tuple[UUID, UUID]] = []

    async def ejecutar(self, cliente_id: UUID, dispositivo_id: UUID) -> None:
        self.llamadas.append((cliente_id, dispositivo_id))
        if self.error is not None:
            raise self.error


def _app(registrar: Any = None, dar_de_baja: Any = None) -> FastAPI:
    app = FastAPI()
    registrar_manejadores(app)
    app.include_router(dispositivos.router)
    app.state.registrar_dispositivo = registrar
    app.state.dar_de_baja_dispositivo = dar_de_baja
    return app


def _cliente(**casos: Any) -> TestClient:
    return TestClient(_app(**casos))


# --- Seguridad: mismo patrón que consulta (401 sin actor, 403 con otro rol) --------------------
def test_registrar_sin_actor_es_401() -> None:
    cliente = _cliente(registrar=_RegistrarFalso())
    respuesta = cliente.post(f"/v1/clientes/{CLIENTE}/dispositivos", json={"token": TOKEN})
    assert respuesta.status_code == 401
    assert respuesta.json()["codigo"] == "ACTOR_REQUERIDO"


def test_registrar_con_otro_rol_es_403() -> None:
    cliente = _cliente(registrar=_RegistrarFalso())
    respuesta = cliente.post(
        f"/v1/clientes/{CLIENTE}/dispositivos",
        json={"token": TOKEN},
        headers={"X-Actor-Id": str(CLIENTE), "X-Actor-Rol": "ASESOR"},
    )
    assert respuesta.status_code == 403
    assert respuesta.json()["codigo"] == "ROL_NO_AUTORIZADO"


def test_dar_de_baja_sin_actor_es_401() -> None:
    cliente = _cliente(dar_de_baja=_DarDeBajaFalso())
    respuesta = cliente.delete(f"/v1/clientes/{CLIENTE}/dispositivos/{DISPOSITIVO}")
    assert respuesta.status_code == 401


def test_dar_de_baja_con_otro_rol_es_403() -> None:
    cliente = _cliente(dar_de_baja=_DarDeBajaFalso())
    respuesta = cliente.delete(
        f"/v1/clientes/{CLIENTE}/dispositivos/{DISPOSITIVO}",
        headers={"X-Actor-Id": str(CLIENTE), "X-Actor-Rol": "ASESOR"},
    )
    assert respuesta.status_code == 403


# --- Registro -----------------------------------------------------------------------------------
def test_registro_responde_201_con_el_dispositivoId() -> None:
    caso = _RegistrarFalso()
    cliente = _cliente(registrar=caso)
    respuesta = cliente.post(
        f"/v1/clientes/{CLIENTE}/dispositivos", json={"token": TOKEN}, headers=CABECERAS
    )
    assert respuesta.status_code == 201, respuesta.text
    assert respuesta.json() == {"dispositivoId": str(DISPOSITIVO)}
    assert caso.llamadas == [(CLIENTE, TOKEN)]


def test_el_token_no_aparece_en_la_respuesta_ni_en_la_url() -> None:
    cliente = _cliente(registrar=_RegistrarFalso())
    respuesta = cliente.post(
        f"/v1/clientes/{CLIENTE}/dispositivos", json={"token": TOKEN}, headers=CABECERAS
    )
    assert TOKEN not in respuesta.text
    assert TOKEN not in str(respuesta.request.url)


def test_registro_sin_token_es_422() -> None:
    cliente = _cliente(registrar=_RegistrarFalso())
    respuesta = cliente.post(
        f"/v1/clientes/{CLIENTE}/dispositivos", json={"token": ""}, headers=CABECERAS
    )
    assert respuesta.status_code == 422


def test_registro_sin_caso_de_uso_es_503() -> None:
    cliente = _cliente()
    respuesta = cliente.post(
        f"/v1/clientes/{CLIENTE}/dispositivos", json={"token": TOKEN}, headers=CABECERAS
    )
    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "SERVICIO_NO_DISPONIBLE"
    assert TOKEN not in respuesta.text


# --- Baja ---------------------------------------------------------------------------------------
def test_dar_de_baja_propia_es_204() -> None:
    caso = _DarDeBajaFalso()
    cliente = _cliente(dar_de_baja=caso)
    respuesta = cliente.delete(
        f"/v1/clientes/{CLIENTE}/dispositivos/{DISPOSITIVO}", headers=CABECERAS
    )
    assert respuesta.status_code == 204
    assert respuesta.text == ""
    assert caso.llamadas == [(CLIENTE, DISPOSITIVO)]


def test_dar_de_baja_ajena_es_403_sin_datos() -> None:
    caso = _DarDeBajaFalso(error=DispositivoNoAutorizado())
    cliente = _cliente(dar_de_baja=caso)
    respuesta = cliente.delete(
        f"/v1/clientes/{CLIENTE}/dispositivos/{DISPOSITIVO}", headers=CABECERAS
    )
    assert respuesta.status_code == 403
    assert respuesta.json()["codigo"] == "DISPOSITIVO_NO_AUTORIZADO"
    assert respuesta.json()["detalles"] == {}


def test_dar_de_baja_inexistente_es_404() -> None:
    caso = _DarDeBajaFalso(error=DispositivoNoEncontrado())
    cliente = _cliente(dar_de_baja=caso)
    respuesta = cliente.delete(
        f"/v1/clientes/{CLIENTE}/dispositivos/{DISPOSITIVO}", headers=CABECERAS
    )
    assert respuesta.status_code == 404
    assert respuesta.json()["codigo"] == "DISPOSITIVO_NO_ENCONTRADO"


def test_dar_de_baja_sin_caso_de_uso_es_503() -> None:
    cliente = _cliente()
    respuesta = cliente.delete(
        f"/v1/clientes/{CLIENTE}/dispositivos/{DISPOSITIVO}", headers=CABECERAS
    )
    assert respuesta.status_code == 503


def test_solicitud_registro_no_muestra_el_token_en_repr_ni_str() -> None:
    solicitud = dispositivos.SolicitudRegistro(token=TOKEN)
    assert TOKEN not in repr(solicitud)
    assert TOKEN not in str(solicitud)
