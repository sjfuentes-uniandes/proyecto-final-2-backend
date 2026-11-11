"""GET /v1/indemnizaciones?siniestroId= (HU-W23, W23-B13): actor, rol, forma de la respuesta y
errores estables."""

import json
from typing import Any
from uuid import uuid4

import pytest
from dinero.application.indemnizacion.consultar import ConsultarIndemnizaciones
from dinero.config import Settings
from dinero.main import crear_app
from fastapi.testclient import TestClient

ANALISTA = {
    "X-Actor-Id": "a0a0a0a0-0000-4000-8000-000000000001",
    "X-Actor-Rol": "ANALISTA_SINIESTROS",
}


class Lectura:
    def __init__(self, filas: list[Any]) -> None:
        self.filas = filas
        self.pedidos: list[Any] = []

    async def por_siniestro(self, siniestro_id: Any) -> list[Any]:
        self.pedidos.append(siniestro_id)
        return self.filas


def _cliente(filas: list[Any]) -> tuple[TestClient, Lectura]:
    app = crear_app(Settings())
    lectura = Lectura(filas)
    cliente = TestClient(app)
    cliente.__enter__()
    app.state.consultar = ConsultarIndemnizaciones(lectura)
    return cliente, lectura


def test_sin_indemnizacion_responde_200_con_la_lista_vacia() -> None:
    cliente, lectura = _cliente([])
    siniestro = uuid4()
    respuesta = cliente.get(
        "/v1/indemnizaciones", params={"siniestroId": str(siniestro)}, headers=ANALISTA
    )
    assert respuesta.status_code == 200 and respuesta.json() == {"items": []}
    assert lectura.pedidos == [siniestro]


def test_una_pendiente_confirmada_y_rechazada_en_la_forma_del_contrato(d: Any) -> None:
    pendiente = d.nueva().registrar_incierto("ADAPTADOR_TIEMPO_AGOTADO", d.DESPUES)
    confirmada = d.nueva().registrar_duplicado().confirmar("TRX-1", d.DESPUES)
    rechazada = d.nueva().rechazar("BENEFICIARIO_NO_HABILITADO", d.DESPUES)
    cliente, _ = _cliente([pendiente, confirmada, rechazada])
    respuesta = cliente.get(
        "/v1/indemnizaciones", params={"siniestroId": str(uuid4())}, headers=ANALISTA
    )
    p, c, r = respuesta.json()["items"]
    assert (p["estado"], p["intentos"], p["confirmadaEn"]) == ("PENDIENTE", 1, None)
    assert p["ultimoIntentoEn"] == "2026-11-10T15:00:02Z" and p["transaccionId"] is None
    assert (c["estado"], c["transaccionId"], c["confirmadaEn"], c["duplicadosDescartados"]) == (
        "CONFIRMADA",
        "TRX-1",
        "2026-11-10T15:00:02Z",
        1,
    )
    assert c["ultimoIntentoEn"] is None
    assert (r["estado"], r["motivo"], r["confirmadaEn"]) == (
        "RECHAZADA",
        "BENEFICIARIO_NO_HABILITADO",
        None,
    )
    for item in (p, c, r):
        assert item["tipo"] == "INDEMNIZACION" and item["monto"] == "2500000.00"
        assert item["moneda"] == "COP" and item["referencia"] == "IND-88-2026-0442-01"
        assert item["creadaEn"] == "2026-11-10T15:00:00Z"
        assert set(item) == {
            "id",
            "siniestroId",
            "tipo",
            "referencia",
            "monto",
            "moneda",
            "estado",
            "transaccionId",
            "motivo",
            "intentos",
            "duplicadosDescartados",
            "creadaEn",
            "confirmadaEn",
            "ultimoIntentoEn",
        }


def test_la_respuesta_no_lleva_datos_de_cuenta(d: Any) -> None:
    cliente, _ = _cliente(
        [d.nueva(beneficiario_ref="ben-sint-centinela").confirmar("T", d.DESPUES)]
    )
    respuesta = cliente.get(
        "/v1/indemnizaciones", params={"siniestroId": str(uuid4())}, headers=ANALISTA
    )
    assert (
        "ben-sint-centinela" not in json.dumps(respuesta.json())
        and "beneficiario" not in respuesta.text
    )


def test_el_monto_conserva_los_decimales(d: Any) -> None:
    from decimal import Decimal

    cliente, _ = _cliente([d.nueva(monto=Decimal("2850000.5")).confirmar("T", d.DESPUES)])
    respuesta = cliente.get(
        "/v1/indemnizaciones", params={"siniestroId": str(uuid4())}, headers=ANALISTA
    )
    assert respuesta.json()["items"][0]["monto"] == "2850000.5"


@pytest.mark.parametrize(
    ("encabezados", "estado", "codigo"),
    [
        ({}, 401, "ACTOR_REQUERIDO"),
        ({"X-Actor-Rol": "ANALISTA_SINIESTROS"}, 401, "ACTOR_REQUERIDO"),
        ({"X-Actor-Id": " "}, 401, "ACTOR_REQUERIDO"),
        ({"X-Actor-Id": "x" * 129, "X-Actor-Rol": "ANALISTA_SINIESTROS"}, 401, "ACTOR_REQUERIDO"),
        ({"X-Actor-Id": "a1"}, 403, "ROL_NO_AUTORIZADO"),
        ({"X-Actor-Id": "a1", "X-Actor-Rol": "ASESOR"}, 403, "ROL_NO_AUTORIZADO"),
    ],
)
def test_sin_actor_o_con_otro_rol_se_rechaza(
    encabezados: dict[str, str], estado: int, codigo: str
) -> None:
    cliente, lectura = _cliente([])
    respuesta = cliente.get(
        "/v1/indemnizaciones", params={"siniestroId": str(uuid4())}, headers=encabezados
    )
    assert respuesta.status_code == estado
    assert respuesta.json()["codigo"] == codigo and lectura.pedidos == []


@pytest.mark.parametrize("params", [{}, {"siniestroId": "no-uuid"}])
def test_el_siniestro_es_obligatorio_y_uuid(params: dict[str, str]) -> None:
    cliente, _ = _cliente([])
    respuesta = cliente.get("/v1/indemnizaciones", params=params, headers=ANALISTA)
    assert respuesta.status_code == 422
    assert respuesta.json()["codigo"] == "SOLICITUD_INVALIDA"
    assert respuesta.json()["detalles"]["campos"] == ["siniestroId"]


def test_sin_base_responde_503_dependencia_no_disponible() -> None:
    with TestClient(crear_app(Settings())) as cliente:
        respuesta = cliente.get(
            "/v1/indemnizaciones", params={"siniestroId": str(uuid4())}, headers=ANALISTA
        )
        assert respuesta.status_code == 503
        assert respuesta.json()["codigo"] == "DEPENDENCIA_NO_DISPONIBLE"
        assert cliente.get("/health").json() == {"status": "ok"}


def test_la_consulta_se_mide_y_el_log_lleva_solo_el_siniestro(capturar_logs: Any, d: Any) -> None:
    cliente, _ = _cliente(
        [d.nueva(beneficiario_ref="ben-sint-centinela").confirmar("T", d.DESPUES)]
    )
    logs = capturar_logs()  # después de crear la app: instrumentar() reconfigura el logging
    siniestro = str(uuid4())
    cliente.get("/v1/indemnizaciones", params={"siniestroId": siniestro}, headers=ANALISTA)
    [medicion] = [r for r in logs.registros() if r["operation"] == "indemnizacion.consultar"]
    assert medicion["result"] == "ok"
    [cierre] = [r for r in logs.registros() if r["message"] == "indemnizacion consultada"]
    assert cierre["siniestro_id"] == siniestro
    assert "ben-sint-centinela" not in logs.texto() and "TRX" not in logs.texto()
