"""N2 HU-W13: insumos de la oferta para la suscripción con catalogo, clientes y PostgreSQL efímeros
(arnés de conftest.py): perfiles reales del simulador, identidad real de clientes y vigencia de la
oferta (DS2-20). Los logs de la suite los revisa test_zz_centinelas.py."""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

pytest.importorskip("docker")

ASESOR = {"X-Actor-Id": "asesor-sintetico", "X-Actor-Rol": "ASESOR"}
DATOS_C01 = {"fechaNacimiento": "1990-04-18", "plazoCreditoMeses": 180, "sumaAsegurada": 180000000}


def _cliente(nn: str) -> str:
    return f"00000000-0000-4000-8000-0000000000{nn}"


def _cotizar(app: Any, nn: str, perfil_id: str | None = None) -> dict[str, Any]:
    cuerpo: dict[str, Any] = {
        "clienteId": _cliente(nn),
        "productoId": "vida-hipotecario",
        "datosAsegurado": DATOS_C01,
    }
    if perfil_id:
        cuerpo["perfilId"] = perfil_id
    respuesta = app.cliente.post(
        "/v1/cotizaciones", json=cuerpo, headers={**ASESOR, "Idempotency-Key": str(uuid4())}
    )
    assert respuesta.status_code == 201, respuesta.text
    creada: dict[str, Any] = respuesta.json()
    return creada


def _insumos(app: Any, cotizacion_id: str) -> Any:
    return app.cliente.get(f"/v1/cotizaciones/{cotizacion_id}/insumos-suscripcion", headers=ASESOR)


def test_insumos_con_el_perfil_real(cotizacion: Callable[..., Any]) -> None:
    app = cotizacion()
    perfil = app.enviar("01")
    assert perfil.status_code == 201, perfil.text
    creada = _cotizar(app, "01", perfil.json()["id"])
    respuesta = _insumos(app, creada["id"])
    assert respuesta.status_code == 200, respuesta.text
    insumos = respuesta.json()
    assert insumos["estadoIdentidad"] == "VERIFICADO"
    assert (insumos["edad"], insumos["plazoCreditoMeses"], insumos["sumaAsegurada"]) == (
        36,
        180,
        "180000000",
    )
    assert insumos["perfil"]["nivelRiesgo"] == perfil.json()["nivelRiesgo"]
    assert float(insumos["perfil"]["confianza"]) == perfil.json()["confianza"]
    assert (insumos["perfil"]["fuentesConsultadas"], insumos["perfil"]["fuentesDeclaradas"]) == (
        4,
        4,
    )
    assert insumos["coincidenciasListasRestrictivas"] == 0
    calculado = datetime.fromisoformat(insumos["calculadoEn"])
    # HU-W04: la vigencia es la de las reglas vigentes (2026.10.1: 7 días), guardada con la oferta.
    assert datetime.fromisoformat(insumos["vigenteHasta"]) == calculado + timedelta(days=7)
    assert insumos["vigenteHasta"] == creada["vigenteHasta"] == creada["oferta"]["vigenteHasta"]
    assert (insumos["ofertaId"], insumos["codigo"]) == (
        creada["oferta"]["id"],
        creada["oferta"]["codigo"],
    )
    assert "fechaNacimiento" not in respuesta.text


def test_coincidencias_en_listas_restrictivas(cotizacion: Callable[..., Any]) -> None:
    """C14: una coincidencia en DA_REGISTROS_PUBLICOS llega a los insumos (SUS-015)."""
    app = cotizacion()
    perfil = app.enviar("0e")
    assert perfil.status_code == 201, perfil.text
    creada = _cotizar(app, "0e", perfil.json()["id"])
    assert _insumos(app, creada["id"]).json()["coincidenciasListasRestrictivas"] == 1


@pytest.mark.parametrize(("nn", "estado"), [("0b", "RECHAZADO"), ("0c", "PENDIENTE")])
def test_identidad_no_verificada_se_informa(
    cotizacion: Callable[..., Any], nn: str, estado: str
) -> None:
    """DS2-17: cotizar no exige identidad; los insumos la informan y polizas decide."""
    app = cotizacion()
    insumos = _insumos(app, _cotizar(app, nn)["id"]).json()
    assert (insumos["estadoIdentidad"], insumos["perfil"]) == (estado, None)


def test_otro_parametro_de_vigencia(cotizacion: Callable[..., Any]) -> None:
    """HU-W04: las reglas con condiciones de oferta mandan sobre el parámetro de HU-W13. Con 0
    horas la oferta ya no nace vencida: vence a los 7 días de las reglas 2026.10.1. El parámetro
    queda para las reglas sin condiciones (tests/unit/cotizacion/test_oferta.py)."""
    app = cotizacion(oferta_vigencia_horas=0)
    insumos = _insumos(app, _cotizar(app, "01")["id"]).json()
    calculado = datetime.fromisoformat(insumos["calculadoEn"])
    assert datetime.fromisoformat(insumos["vigenteHasta"]) == calculado + timedelta(days=7)
