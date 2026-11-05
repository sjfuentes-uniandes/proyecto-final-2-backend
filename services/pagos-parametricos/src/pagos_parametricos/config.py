from pydantic import AliasChoices, Field
from solventa_common.settings import ServiceSettings


def _env(nombre: str) -> AliasChoices:
    # Nombre que entrega la infraestructura y su variante SOLVENTA_ (S1-A6).
    return AliasChoices(nombre, f"SOLVENTA_{nombre}")


class Settings(ServiceSettings):
    service_name: str = "pagos-parametricos"

    # HU-S03: un evento paramétrico válido crea el siniestro una sola vez
    # (PagosParametricos --> AtencionSiniestros, modelo_concurrencia.puml (9)).
    siniestros_url: str = Field("http://localhost:8091", validation_alias=_env("SINIESTROS_URL"))
    # Eventos observados y copia local de las coberturas activas (eventos Poliza*).
    sqs_parametricos_eventos_url: str | None = Field(
        None, validation_alias=_env("SQS_PARAMETRICOS_EVENTOS_URL")
    )
    sqs_parametricos_polizas_url: str | None = Field(
        None, validation_alias=_env("SQS_PARAMETRICOS_POLIZAS_URL")
    )


settings = Settings()
