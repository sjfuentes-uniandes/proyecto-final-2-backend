from pydantic import AliasChoices, Field
from solventa_common.settings import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "bff-web"

    aws_region: str = Field("us-east-1", validation_alias=AliasChoices("AWS_REGION", "SOLVENTA_AWS_REGION"))
    # Emisores válidos por pool de Cognito (infra/apps/ecs.tf, tier acceso).
    jwt_issuers: dict[str, str] = Field(default_factory=dict, validation_alias=AliasChoices("JWT_ISSUERS", "SOLVENTA_JWT_ISSUERS"))
    grupo_operacion: str = "operacion"
    grupo_administradores: str = "administradores"
    # Pool de Cognito del back-office (infra/apps/ecs.tf): Administración › Usuarios.
    backoffice_user_pool_id: str | None = Field(
        None, validation_alias=AliasChoices("BACKOFFICE_USER_POOL_ID", "SOLVENTA_BACKOFFICE_USER_POOL_ID")
    )
    # Orígenes del portal (JSON): la URL de CloudFront en AWS y Angular en local.
    cors_origins: list[str] = Field(["http://localhost:4200"], validation_alias=AliasChoices("CORS_ORIGINS", "SOLVENTA_CORS_ORIGINS"))
    # HU-W27: límite del recorrido que usa la interpretación de la traza (mockup: 1,5 s).
    umbral_recorrido_ms: int = 1500


settings = Settings()
