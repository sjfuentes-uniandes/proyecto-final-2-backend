from pydantic import AliasChoices, Field
from solventa_common.settings import ServiceSettings


def _env(nombre: str) -> AliasChoices:
    # Nombre que entrega la infraestructura y su variante SOLVENTA_ (S1-A6).
    return AliasChoices(nombre, f"SOLVENTA_{nombre}")


class Settings(ServiceSettings):
    service_name: str = "siniestros"

    # HU-S03: siniestros es el único servicio con acceso al bucket de evidencias
    # (evidence_writers y evidence_readers, infra/apps/iam.tf). La política del bucket
    # rechaza un PutObject sin el encabezado de cifrado EVIDENCIAS_BUCKET_SSE.
    evidencias_bucket: str | None = Field(None, validation_alias=_env("EVIDENCIAS_BUCKET"))
    evidencias_bucket_sse: str = Field("AES256", validation_alias=_env("EVIDENCIAS_BUCKET_SSE"))
    # Resultado de la indemnización (modelo_concurrencia.puml (8)); sin la cola no arranca el consumidor.
    sqs_siniestros_indemnizaciones_url: str | None = Field(
        None, validation_alias=_env("SQS_SINIESTROS_INDEMNIZACIONES_URL")
    )


settings = Settings()
