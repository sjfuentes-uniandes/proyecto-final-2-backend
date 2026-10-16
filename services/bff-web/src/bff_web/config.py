from typing import Any

from pydantic import AliasChoices, Field
from solventa_common.settings import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "bff-web"

    aws_region: str = Field("us-east-1", validation_alias=AliasChoices("AWS_REGION", "SOLVENTA_AWS_REGION"))
    # Emisores válidos por pool de Cognito (infra/apps/ecs.tf, tier acceso).
    jwt_issuers: dict[str, str] = Field(default_factory=dict, validation_alias=AliasChoices("JWT_ISSUERS", "SOLVENTA_JWT_ISSUERS"))
    grupo_operacion: str = "operacion"
    grupo_administradores: str = "administradores"
    grupo_administradores_socios: str = "administradores-socios"
    grupo_asesores: str = "asesores"
    api_socios_url: str = Field("http://localhost:8082", validation_alias=AliasChoices("API_SOCIOS_URL", "SOLVENTA_API_SOCIOS_URL"))
    # HU-W11: perfilamiento en cotizacion (01 §3.2).
    cotizacion_url: str = Field("http://localhost:8085", validation_alias=AliasChoices("COTIZACION_URL", "SOLVENTA_COTIZACION_URL"))
    cotizacion_timeout_ms: int = 1500
    # Pool de Cognito del back-office (infra/apps/ecs.tf): Administración › Usuarios.
    backoffice_user_pool_id: str | None = Field(
        None, validation_alias=AliasChoices("BACKOFFICE_USER_POOL_ID", "SOLVENTA_BACKOFFICE_USER_POOL_ID")
    )
    # Orígenes del portal (JSON): la URL de CloudFront en AWS y Angular en local.
    cors_origins: list[str] = Field(["http://localhost:4200"], validation_alias=AliasChoices("CORS_ORIGINS", "SOLVENTA_CORS_ORIGINS"))
    # HU-W27: límite del recorrido que usa la interpretación de la traza (mockup: 1,5 s).
    umbral_recorrido_ms: int = 1500
    # HU-W28: alarmas del ambiente, generadas por Terraform (infra/apps/alarms.tf).
    alertas_prefijo: str = Field("solventa-local-", validation_alias=AliasChoices("ALERTAS_PREFIJO", "SOLVENTA_ALERTAS_PREFIJO"))
    alertas_catalogo: dict[str, dict[str, Any]] = Field(
        default_factory=dict, validation_alias=AliasChoices("ALERTAS_CATALOGO", "SOLVENTA_ALERTAS_CATALOGO")
    )
    alertas_umbrales: list[dict[str, Any]] = Field(
        default_factory=list, validation_alias=AliasChoices("ALERTAS_UMBRALES", "SOLVENTA_ALERTAS_UMBRALES")
    )
    alertas_advertencia_pct: float = Field(
        90, validation_alias=AliasChoices("ALERTAS_ADVERTENCIA_PCT", "SOLVENTA_ALERTAS_ADVERTENCIA_PCT")
    )
    alertas_atencion_min: float = Field(30, validation_alias=AliasChoices("ALERTAS_ATENCION_MIN", "SOLVENTA_ALERTAS_ATENCION_MIN"))
    alertas_falso_positivo_min: float = Field(
        5, validation_alias=AliasChoices("ALERTAS_FALSO_POSITIVO_MIN", "SOLVENTA_ALERTAS_FALSO_POSITIVO_MIN")
    )


settings = Settings()
