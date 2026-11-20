from typing import Any

from solventa_common.ubicacion import UbicacionCapturada, UbicacionReporte

from siniestros.application.siniestro.errores import SolicitudInvalida
from siniestros.config import Settings


def evaluar_ubicacion(lectura: UbicacionCapturada, maximo_m: float) -> UbicacionReporte:
    estado = (
        "NO_DISPONIBLE"
        if lectura.precision_m is None
        else "DISPONIBLE"
        if lectura.precision_m <= maximo_m
        else "NO_CONFIABLE"
    )
    return UbicacionReporte(**lectura.model_dump(), estado=estado)


def reevaluar_ubicacion(
    ubicacion: dict[str, Any] | None, maximo_m: float
) -> UbicacionReporte | None:
    if ubicacion is None:
        return None
    lectura = UbicacionCapturada.model_validate(
        {k: v for k, v in ubicacion.items() if k != "estado"}
    )
    return evaluar_ubicacion(lectura, maximo_m)


def validar_ubicacion_envio(
    ubicacion: dict[str, Any] | None, causa: str | None, settings: Settings
) -> None:
    if not settings.ubicacion_obligatoria or causa in settings.ubicacion_tipos_opcionales:
        return
    actual = reevaluar_ubicacion(ubicacion, settings.ubicacion_precision_max_m)
    if actual is None or actual.estado != "DISPONIBLE":
        raise SolicitudInvalida(["ubicacion"])
