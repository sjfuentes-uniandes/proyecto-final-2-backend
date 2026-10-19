from pydantic import AliasChoices, Field
from solventa_common.settings import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "catalogo"

    # Archivo de adapters/outbound/semillas/qa/ (sin .json) que se publica al arrancar (D-04 a).
    # Solo lo define una rama qa/ sin fusionar, durante la demo de W11 AC4.
    semilla_qa: str | None = Field(
        None,
        pattern=r"^[a-z0-9_]*$",
        validation_alias=AliasChoices("CATALOGO_SEMILLA_QA", "SOLVENTA_CATALOGO_SEMILLA_QA"),
    )


settings = Settings()
