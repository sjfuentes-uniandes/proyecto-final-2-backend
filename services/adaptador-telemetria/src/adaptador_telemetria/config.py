from pydantic import AliasChoices, Field
from solventa_common.settings import ServiceSettings


def _env(nombre: str) -> AliasChoices:
    # Nombre que entrega la infraestructura y su variante SOLVENTA_ (S1-A6).
    return AliasChoices(nombre, f"SOLVENTA_{nombre}")


class Settings(ServiceSettings):
    service_name: str = "adaptador-telemetria"

    # HU-S03: el original de cada evento se archiva en el bucket de evidencias, solo bajo
    # TELEMETRIA_ARCHIVO_PREFIJO (telemetria_archivers, infra/apps/iam.tf).
    evidencias_bucket: str | None = Field(None, validation_alias=_env("EVIDENCIAS_BUCKET"))
    evidencias_bucket_sse: str = Field("AES256", validation_alias=_env("EVIDENCIAS_BUCKET_SSE"))
    telemetria_archivo_prefijo: str = Field(
        "telemetria/", validation_alias=_env("TELEMETRIA_ARCHIVO_PREFIJO")
    )


settings = Settings()
