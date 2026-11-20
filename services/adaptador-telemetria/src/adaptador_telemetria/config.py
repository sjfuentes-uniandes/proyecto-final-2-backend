import json
from typing import Annotated

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import NoDecode
from solventa_common import outbox
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

    # HU-W21 (DS3-16). JSON `{"hmac_secret": …}` que inyecta la infraestructura desde Secrets
    # Manager; en compose.yaml, un valor sintético. Nunca se registra (SecretStr) ni se publica.
    ally_telemetria_credentials: SecretStr | None = Field(
        None, validation_alias=_env("ALLY_TELEMETRIA_CREDENTIALS")
    )
    # Fuentes de telemetría autorizadas (lista separada por comas). Una fuente fuera de la lista
    # recibe el mismo 401 que una firma inválida.
    telemetria_fuentes: Annotated[tuple[str, ...], NoDecode] = Field(
        ("IDEAM-SIM", "AEROCIVIL-SIM"), validation_alias=_env("TELEMETRIA_FUENTES")
    )
    # Desfase máximo (en cualquier sentido) entre la marca de tiempo firmada y el reloj (D5).
    desfase_max_s: int = Field(300, ge=1)
    # Corte del PutObject del original (§13, regla 7: nunca más de 700 ms).
    s3_timeout_ms: int = Field(300, ge=1, le=700)

    aws_region: str = Field("us-east-1", validation_alias=_env("AWS_REGION"))
    # Solo local y pruebas (LocalStack, compose.yaml); en AWS no se define.
    aws_endpoint_url: str | None = Field(None, validation_alias=_env("AWS_ENDPOINT_URL"))
    # Outbox hacia el tópico de eventos de negocio (HU-S03).
    outbox_enabled: bool = Field(False, validation_alias=_env("OUTBOX_ENABLED"))
    events_topic_arn: str | None = Field(None, validation_alias=_env("EVENTS_TOPIC_ARN"))
    # Valores por defecto de solventa_common.outbox.
    outbox_lote: int = Field(outbox.LOTE, ge=1)
    outbox_intervalo_s: float = Field(outbox.INTERVALO_S, gt=0)
    outbox_max_intentos: int = Field(outbox.MAX_INTENTOS, ge=1)

    @field_validator("telemetria_fuentes", mode="before")
    @classmethod
    def _lista_de_fuentes(cls, valor: object) -> object:
        if isinstance(valor, str):
            return tuple(fuente.strip() for fuente in valor.split(",") if fuente.strip())
        return valor

    @property
    def hmac_secret(self) -> str | None:
        """El `hmac_secret` de las credenciales, o None si faltan o no son el JSON esperado: sin
        secreto no se acepta ningún envío (falla cerrada)."""
        if self.ally_telemetria_credentials is None:
            return None
        try:
            credenciales = json.loads(self.ally_telemetria_credentials.get_secret_value())
        except ValueError:
            return None
        secreto = credenciales.get("hmac_secret") if isinstance(credenciales, dict) else None
        return secreto if isinstance(secreto, str) and secreto else None


settings = Settings()
