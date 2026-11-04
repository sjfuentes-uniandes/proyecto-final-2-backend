from pydantic import AliasChoices, Field, model_validator
from solventa_common import inbox
from solventa_common.settings import ServiceSettings

AMBIENTES_CON_SEMILLA = frozenset({"int", "local"})


def _env(nombre: str) -> AliasChoices:
    # Nombre que entrega la infraestructura y su variante SOLVENTA_.
    return AliasChoices(nombre, f"SOLVENTA_{nombre}")


class Settings(ServiceSettings):
    service_name: str = "consulta"

    # HU-M10: la proyección lee el producto vigente de catalogo al proyectar. catalog.tf agrega
    # catalogo a consulta.calls (Fase 4).
    catalogo_url: str = Field("http://localhost:8084", validation_alias=_env("CATALOGO_URL"))
    catalogo_timeout_ms: int = Field(300, ge=1, le=700)
    aws_region: str = Field("us-east-1", validation_alias=_env("AWS_REGION"))
    # Solo local y pruebas (LocalStack, compose.yaml); en AWS no se define.
    aws_endpoint_url: str | None = Field(None, validation_alias=_env("AWS_ENDPOINT_URL"))
    # La infraestructura entrega la cola a consulta (`consumes`); sin ella (local y pruebas) el
    # consumidor no arranca. La espera larga de 10 s deja caber la parada en el stopTimeout de ECS.
    sqs_consulta_proyeccion_url: str | None = Field(
        None, validation_alias=_env("SQS_CONSULTA_PROYECCION_URL")
    )
    # HU-W18: una póliza vigente con este número de días o menos para terminar está POR_VENCER.
    dias_por_vencer: int = Field(30, ge=1, le=365, validation_alias=_env("DIAS_POR_VENCER"))
    # Siembra pólizas de prueba al arrancar. Solo int y local.
    semilla_polizas: bool = Field(False, validation_alias=_env("CONSULTA_SEMILLA_POLIZAS"))
    consumidor_max_mensajes: int = Field(inbox.MAX_MENSAJES, ge=1, le=10)
    consumidor_espera_larga_s: int = Field(10, ge=1, le=10)

    @model_validator(mode="after")
    def _semilla_solo_en_pruebas(self) -> "Settings":
        if self.semilla_polizas and self.environment not in AMBIENTES_CON_SEMILLA:
            raise ValueError("CONSULTA_SEMILLA_POLIZAS solo se permite en int o local")
        return self


settings = Settings()
