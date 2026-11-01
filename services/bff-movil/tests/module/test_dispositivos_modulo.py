"""Endpoints /v1/dispositivos con el adaptador de `avisos` simulado (HU-M11)."""

import logging
from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest
from bff_movil.domain.dispositivos.modelos import DispositivoNoAutorizado, DispositivoNoEncontrado
from bff_movil.domain.sesion.errores import ServicioNoDisponible

Entorno = Any
CLIENTE_ID = UUID("00000000-0000-4000-8000-000000000001")
DISPOSITIVO_ID = UUID("00000000-0000-4000-8000-0000000000d1")
TOKEN = "token-sintetico-entregado"
CUERPO = {"token": TOKEN}


class GestionFalsa:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.registros: list[tuple[UUID, str, str]] = []
        self.bajas: list[tuple[UUID, UUID, str]] = []

    async def registrar(self, cliente_id: UUID, token: str, actor_id: str) -> UUID:
        self.registros.append((cliente_id, token, actor_id))
        if self.error:
            raise self.error
        return DISPOSITIVO_ID

    async def dar_de_baja(self, cliente_id: UUID, dispositivo_id: UUID, actor_id: str) -> None:
        self.bajas.append((cliente_id, dispositivo_id, actor_id))
        if self.error:
            raise self.error


def test_registra_el_dispositivo_del_titular(crear_entorno: Callable[..., Entorno]) -> None:
    gestion = GestionFalsa()
    entorno = crear_entorno(gestion_dispositivos=gestion)

    respuesta = entorno.pedir("POST", "/v1/dispositivos", cuerpo=CUERPO)

    assert respuesta.status_code == 201
    assert respuesta.json() == {"dispositivoId": str(DISPOSITIVO_ID)}
    assert TOKEN not in respuesta.text
    assert gestion.registros == [(CLIENTE_ID, TOKEN, entorno.sub)]


def test_da_de_baja_el_dispositivo_propio(crear_entorno: Callable[..., Entorno]) -> None:
    gestion = GestionFalsa()
    entorno = crear_entorno(gestion_dispositivos=gestion)

    respuesta = entorno.pedir("DELETE", f"/v1/dispositivos/{DISPOSITIVO_ID}")

    assert respuesta.status_code == 204
    assert respuesta.content == b""
    assert gestion.bajas == [(CLIENTE_ID, DISPOSITIVO_ID, entorno.sub)]


@pytest.mark.parametrize(
    ("error", "estado", "codigo"),
    [
        (DispositivoNoAutorizado(), 403, "DISPOSITIVO_NO_AUTORIZADO"),
        (DispositivoNoEncontrado(), 404, "DISPOSITIVO_NO_ENCONTRADO"),
        (ServicioNoDisponible("ERROR"), 503, "SERVICIO_NO_DISPONIBLE"),
        (ServicioNoDisponible("TIEMPO_AGOTADO"), 503, "SERVICIO_NO_DISPONIBLE"),
    ],
)
def test_baja_traduce_los_errores_sin_datos_del_dispositivo(
    crear_entorno: Callable[..., Entorno], error: Exception, estado: int, codigo: str
) -> None:
    entorno = crear_entorno(gestion_dispositivos=GestionFalsa(error))

    respuesta = entorno.pedir("DELETE", f"/v1/dispositivos/{DISPOSITIVO_ID}")

    assert respuesta.status_code == estado
    assert respuesta.json()["codigo"] == codigo
    assert str(DISPOSITIVO_ID) not in respuesta.text


def test_registro_con_caida_responde_503(crear_entorno: Callable[..., Entorno]) -> None:
    entorno = crear_entorno(gestion_dispositivos=GestionFalsa(ServicioNoDisponible("ERROR")))

    respuesta = entorno.pedir("POST", "/v1/dispositivos", cuerpo=CUERPO)

    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "SERVICIO_NO_DISPONIBLE"
    assert TOKEN not in respuesta.text


def test_cliente_inexistente_falla_cerrado_con_404(crear_entorno: Callable[..., Entorno]) -> None:
    gestion = GestionFalsa()
    entorno = crear_entorno(con_cliente=False, gestion_dispositivos=gestion)

    for metodo, ruta, cuerpo in (
        ("POST", "/v1/dispositivos", CUERPO),
        ("DELETE", f"/v1/dispositivos/{DISPOSITIVO_ID}", None),
    ):
        respuesta = entorno.pedir(metodo, ruta, cuerpo=cuerpo)
        assert respuesta.status_code == 404
        assert respuesta.json()["codigo"] == "CLIENTE_NO_ENCONTRADO"
    assert gestion.registros == [] and gestion.bajas == []


def test_sin_token_responde_401(crear_entorno: Callable[..., Entorno]) -> None:
    gestion = GestionFalsa()
    entorno = crear_entorno(gestion_dispositivos=gestion)

    assert entorno.cliente.post("/v1/dispositivos", json=CUERPO).status_code == 401
    assert entorno.cliente.delete(f"/v1/dispositivos/{DISPOSITIVO_ID}").status_code == 401
    assert gestion.registros == [] and gestion.bajas == []


def test_el_sub_del_token_distinto_al_encabezado_responde_401(
    crear_entorno: Callable[..., Entorno],
) -> None:
    gestion = GestionFalsa()
    entorno = crear_entorno(gestion_dispositivos=gestion)

    registro = entorno.pedir("POST", "/v1/dispositivos", sub="otro-titular", cuerpo=CUERPO)
    baja = entorno.pedir("DELETE", f"/v1/dispositivos/{DISPOSITIVO_ID}", sub="otro-titular")

    assert (registro.status_code, baja.status_code) == (401, 401)
    assert TOKEN not in registro.text
    assert gestion.registros == [] and gestion.bajas == []


@pytest.mark.parametrize("cuerpo", [{}, {"token": ""}, {"token": 5}])
def test_el_token_es_obligatorio_y_el_422_no_lo_repite(
    crear_entorno: Callable[..., Entorno], cuerpo: dict[str, Any]
) -> None:
    gestion = GestionFalsa()
    entorno = crear_entorno(gestion_dispositivos=gestion)

    respuesta = entorno.pedir("POST", "/v1/dispositivos", cuerpo=cuerpo)

    assert respuesta.status_code == 422
    assert gestion.registros == []


def test_el_identificador_debe_ser_uuid(crear_entorno: Callable[..., Entorno]) -> None:
    entorno = crear_entorno(gestion_dispositivos=GestionFalsa())

    assert entorno.pedir("DELETE", "/v1/dispositivos/no-es-uuid").status_code == 422


def test_se_ignora_el_cliente_id_enviado_por_el_movil(
    crear_entorno: Callable[..., Entorno],
) -> None:
    gestion = GestionFalsa()
    entorno = crear_entorno(gestion_dispositivos=gestion)
    ajeno = "00000000-0000-4000-8000-0000000000ff"

    entorno.pedir(
        "POST",
        "/v1/dispositivos",
        cuerpo={"token": TOKEN, "clienteId": ajeno},
        params={"clienteId": ajeno},
        encabezados={"X-Cliente-Id": ajeno, "X-Actor-Id": ajeno},
    )
    entorno.pedir(
        "DELETE",
        f"/v1/dispositivos/{DISPOSITIVO_ID}",
        params={"clienteId": ajeno},
        encabezados={"X-Cliente-Id": ajeno},
    )

    assert gestion.registros == [(CLIENTE_ID, TOKEN, entorno.sub)]
    assert gestion.bajas == [(CLIENTE_ID, DISPOSITIVO_ID, entorno.sub)]


def test_no_registra_el_token_ni_el_cliente_id(
    crear_entorno: Callable[..., Entorno], caplog: pytest.LogCaptureFixture
) -> None:
    entorno = crear_entorno(gestion_dispositivos=GestionFalsa())
    caplog.set_level(logging.DEBUG)

    entorno.pedir("POST", "/v1/dispositivos", cuerpo=CUERPO)
    entorno.pedir("POST", "/v1/dispositivos", cuerpo={"token": ""})
    entorno.pedir("DELETE", f"/v1/dispositivos/{DISPOSITIVO_ID}")

    registrado = caplog.text + entorno.telemetria.texto_logs() + entorno.telemetria.texto_spans()
    assert TOKEN not in registrado
    assert str(CLIENTE_ID) not in registrado
