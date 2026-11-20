"""Contrato de ubicacion movil compartido por el BFF y siniestros."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

PermisoUbicacion = Literal[
    "PRECISO", "APROXIMADO", "DENEGADO", "DENEGADO_PERMANENTEMENTE", "NO_SOLICITADO"
]


class UbicacionCapturada(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    permiso: PermisoUbicacion
    lat: float | None = Field(
        default=None, ge=-90, le=90, allow_inf_nan=False, strict=True, repr=False
    )
    lon: float | None = Field(
        default=None, ge=-180, le=180, allow_inf_nan=False, strict=True, repr=False
    )
    precision_m: float | None = Field(default=None, gt=0, allow_inf_nan=False, strict=True)
    capturada_en: AwareDatetime | None = None

    @model_validator(mode="after")
    def _lectura_completa_y_autorizada(self) -> "UbicacionCapturada":
        campos = (self.lat, self.lon, self.precision_m, self.capturada_en)
        if any(c is not None for c in campos):
            if self.permiso not in {"PRECISO", "APROXIMADO"}:
                raise ValueError("una lectura requiere permiso concedido")
            if any(c is None for c in campos):
                raise ValueError("la lectura requiere coordenadas, precision y fecha con zona")
        return self


class UbicacionReporte(UbicacionCapturada):
    estado: Literal["DISPONIBLE", "NO_CONFIABLE", "NO_DISPONIBLE"]
