from pydantic import AliasChoices, Field
from solventa_common import inbox
from solventa_common.settings import ServiceSettings


def _env(nombre: str) -> AliasChoices:
    # Nombre que entrega la infraestructura y su variante SOLVENTA_.
    return AliasChoices(nombre, f"SOLVENTA_{nombre}")


class Settings(ServiceSettings):
    service_name: str = "auditoria"

    aws_region: str = Field("us-east-1", validation_alias=_env("AWS_REGION"))
    # Solo local y pruebas (LocalStack, compose.yaml); en AWS no se define.
    aws_endpoint_url: str | None = Field(None, validation_alias=_env("AWS_ENDPOINT_URL"))
    # La infraestructura entrega la cola (`consumes`) y el bucket (`audit_writers`, iam.tf); sin
    # ellos (local y pruebas) el consumidor no arranca.
    sqs_auditoria_url: str | None = Field(None, validation_alias=_env("SQS_AUDITORIA_URL"))
    audit_bucket: str | None = Field(None, validation_alias=_env("AUDIT_BUCKET"))
    # Cifrado del objeto. Sin valor, el del bucket por defecto (la política solo rechaza otro).
    audit_bucket_sse: str | None = Field(None, validation_alias=_env("AUDIT_BUCKET_SSE"))
    # HU-W24: lote de 10 y espera larga de 20 s. La parada abandona la espera larga, así que no
    # consume el stopTimeout de ECS.
    consumidor_lote: int = Field(inbox.LOTE_SQS, ge=1, le=10)
    consumidor_espera_larga_s: int = Field(inbox.ESPERA_LARGA_S, ge=1, le=20)


settings = Settings()
