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

    # HU-W04: publica semillas/qa/productos_qa.json (productos de prueba, p. ej. la oferta de un
    # minuto del N3 de OFERTA_VENCIDA). Solo en ambientes de QA (infra/envs/int/apps.tfvars).
    productos_qa: bool = Field(
        False,
        validation_alias=AliasChoices("CATALOGO_PRODUCTOS_QA", "SOLVENTA_CATALOGO_PRODUCTOS_QA"),
    )

    # Cache-Aside de productos y reglas de prima (HU-S02, HU-W03). Sin CACHE_URL, sin caché.
    cache_url: str | None = Field(
        None, validation_alias=AliasChoices("CACHE_URL", "SOLVENTA_CACHE_URL"), repr=False
    )
    cache_ttl_s: int = Field(60, ge=0)
    cache_timeout_ms: int = Field(50, ge=1)
    # HU-W05: tope de la lectura de producto y reglas vigentes; con X-Deadline-Ms corta en
    # min(este, 700, deadline − 20) y responde 504 TIEMPO_AGOTADO.
    consulta_timeout_ms: int = Field(500, ge=1)


settings = Settings()
