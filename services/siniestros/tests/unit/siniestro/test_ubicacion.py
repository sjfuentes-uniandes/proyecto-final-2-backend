from typing import Any

import pytest
from pydantic import ValidationError
from siniestros.application.siniestro.errores import SolicitudInvalida
from siniestros.application.siniestro.ubicacion import evaluar_ubicacion, validar_ubicacion_envio
from siniestros.config import Settings
from solventa_common.ubicacion import UbicacionCapturada

LECTURA = {
    "permiso": "PRECISO",
    "lat": 4.6,
    "lon": -74.08,
    "precisionM": 100,
    "capturadaEn": "2026-11-18T10:00:00-05:00",
}


@pytest.mark.parametrize("permiso", ["PRECISO", "APROXIMADO"])
@pytest.mark.parametrize(
    ("precision", "estado"), [(99.99, "DISPONIBLE"), (100, "DISPONIBLE"), (100.01, "NO_CONFIABLE")]
)
def test_precision_inclusiva_y_permiso_aproximado(
    permiso: str, precision: float, estado: str
) -> None:
    lectura = UbicacionCapturada.model_validate(
        {**LECTURA, "permiso": permiso, "precisionM": precision}
    )
    reporte = evaluar_ubicacion(lectura, 100)
    assert reporte.estado == estado
    assert reporte.precision_m == precision
    assert reporte.capturada_en == lectura.capturada_en


@pytest.mark.parametrize(
    "permiso", ["DENEGADO", "DENEGADO_PERMANENTEMENTE", "NO_SOLICITADO", "PRECISO", "APROXIMADO"]
)
def test_ausencia_de_lectura_y_regla_por_tipo(permiso: str) -> None:
    ubicacion = evaluar_ubicacion(UbicacionCapturada.model_validate({"permiso": permiso}), 100)
    assert ubicacion.estado == "NO_DISPONIBLE" and ubicacion.lat is None
    datos = ubicacion.model_dump(by_alias=True, mode="json")
    with pytest.raises(SolicitudInvalida):
        validar_ubicacion_envio(datos, "COLISION", Settings())
    validar_ubicacion_envio(datos, "COLISION", Settings(UBICACION_TIPOS_OPCIONALES=("COLISION",)))
    validar_ubicacion_envio(datos, "COLISION", Settings(UBICACION_OBLIGATORIA=False))


@pytest.mark.parametrize(
    "cambios",
    [
        {"permiso": "DENEGADO"},
        {"permiso": "DENEGADO_PERMANENTEMENTE"},
        {"lat": 91},
        {"lon": -181},
        {"precisionM": 0},
        {"lat": True},
        {"lat": float("nan")},
        {"lon": float("inf")},
        {"precisionM": float("inf")},
        {"lat": None},
        {"capturadaEn": None},
        {"capturadaEn": "2026-11-18T10:00:00"},
        {"estado": "DISPONIBLE"},
    ],
)
def test_rechaza_lectura_incompleta_no_autorizada_o_invalida(cambios: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        UbicacionCapturada.model_validate({**LECTURA, **cambios})


def test_envio_revalida_precision_con_configuracion_actual_sin_confiar_en_estado_del_cliente() -> (
    None
):
    ubicacion = evaluar_ubicacion(UbicacionCapturada.model_validate(LECTURA), 100).model_dump(
        by_alias=True, mode="json"
    )
    validar_ubicacion_envio(ubicacion, "COLISION", Settings())
    with pytest.raises(SolicitudInvalida):
        validar_ubicacion_envio(ubicacion, "COLISION", Settings(UBICACION_PRECISION_MAX_M=99))
    with pytest.raises(SolicitudInvalida):
        validar_ubicacion_envio(None, "COLISION", Settings())


@pytest.mark.parametrize("maximo", [0, -1, float("inf"), float("nan")])
def test_configuracion_de_precision_invalida_no_arranca(maximo: float) -> None:
    with pytest.raises(ValidationError):
        Settings(UBICACION_PRECISION_MAX_M=maximo)
