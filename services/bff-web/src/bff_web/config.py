from typing import Any, Self

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import SettingsConfigDict
from solventa_common import presupuesto
from solventa_common.settings import ServiceSettings

# HU-W05: presupuesto de portal.cotizar si LATENCY_BUDGET_MS no lo trae (latency_budget_ms de
# infra/envs/<env>/apps.tfvars). Debe superar el timeout hacia cotizacion.
PRESUPUESTO_COTIZAR_MS = 2000
OPERACION_COTIZAR = "portal.cotizar"


class Settings(ServiceSettings):
    # Un error de validación al arrancar no debe imprimir la configuración (DB_PASSWORD).
    model_config = SettingsConfigDict(
        env_prefix="SOLVENTA_", extra="ignore", hide_input_in_errors=True
    )

    service_name: str = "bff-web"

    aws_region: str = Field(
        "us-east-1", validation_alias=AliasChoices("AWS_REGION", "SOLVENTA_AWS_REGION")
    )
    # Emisores válidos por pool de Cognito (infra/apps/ecs.tf, tier acceso).
    jwt_issuers: dict[str, str] = Field(
        default_factory=dict, validation_alias=AliasChoices("JWT_ISSUERS", "SOLVENTA_JWT_ISSUERS")
    )
    grupo_operacion: str = "operacion"
    grupo_administradores: str = "administradores"
    grupo_administradores_socios: str = "administradores-socios"
    grupo_asesores: str = "asesores"
    # HU-W13 (DS2-12): bandeja de suscripción (E01) y revisión asistida (E04).
    grupo_suscriptores: str = "suscriptores"
    api_socios_url: str = Field(
        "http://localhost:8082",
        validation_alias=AliasChoices("API_SOCIOS_URL", "SOLVENTA_API_SOCIOS_URL"),
    )
    # HU-W11: perfilamiento en cotizacion (01 §3.2).
    cotizacion_url: str = Field(
        "http://localhost:8085",
        validation_alias=AliasChoices("COTIZACION_URL", "SOLVENTA_COTIZACION_URL"),
    )
    cotizacion_timeout_ms: int = 1500
    # HU-W05: presupuesto por operación (JSON). "portal.cotizar" inicia el X-Deadline-Ms de la
    # cotización del portal hacia cotizacion.
    latency_budget_ms: dict[str, int] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("LATENCY_BUDGET_MS", "SOLVENTA_LATENCY_BUDGET_MS"),
    )
    # HU-W30: alta y verificación de identidad en clientes. El timeout supera el de clientes
    # hacia adaptador-identidad (4 s): el BFF no debe rendirse antes que el proveedor.
    clientes_url: str = Field(
        "http://localhost:8083",
        validation_alias=AliasChoices("CLIENTES_URL", "SOLVENTA_CLIENTES_URL"),
    )
    clientes_timeout_ms: int = 5000
    # HU-W13: suscripción en polizas (DS2-19: como cotizacion_timeout_ms). El nombre del cliente
    # se compone con su propio timeout corto: si clientes tarda, la vista sale sin él.
    polizas_url: str = Field(
        "http://localhost:8088",
        validation_alias=AliasChoices("POLIZAS_URL", "SOLVENTA_POLIZAS_URL"),
    )
    polizas_timeout_ms: int = 1500
    nombre_cliente_timeout_ms: int = 500
    # Pool de Cognito del back-office (infra/apps/ecs.tf): Administración › Usuarios.
    backoffice_user_pool_id: str | None = Field(
        None,
        validation_alias=AliasChoices(
            "BACKOFFICE_USER_POOL_ID", "SOLVENTA_BACKOFFICE_USER_POOL_ID"
        ),
    )
    # Orígenes del portal (JSON): la URL de CloudFront en AWS y Angular en local.
    cors_origins: list[str] = Field(
        ["http://localhost:4200"],
        validation_alias=AliasChoices("CORS_ORIGINS", "SOLVENTA_CORS_ORIGINS"),
    )
    # HU-W27: límite del recorrido que usa la interpretación de la traza (mockup: 1,5 s).
    umbral_recorrido_ms: int = 1500
    # HU-W28: alarmas del ambiente, generadas por Terraform (infra/apps/alarms.tf).
    alertas_prefijo: str = Field(
        "solventa-local-",
        validation_alias=AliasChoices("ALERTAS_PREFIJO", "SOLVENTA_ALERTAS_PREFIJO"),
    )
    alertas_catalogo: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("ALERTAS_CATALOGO", "SOLVENTA_ALERTAS_CATALOGO"),
    )
    alertas_umbrales: list[dict[str, Any]] = Field(
        default_factory=list,
        validation_alias=AliasChoices("ALERTAS_UMBRALES", "SOLVENTA_ALERTAS_UMBRALES"),
    )
    alertas_advertencia_pct: float = Field(
        90,
        validation_alias=AliasChoices(
            "ALERTAS_ADVERTENCIA_PCT", "SOLVENTA_ALERTAS_ADVERTENCIA_PCT"
        ),
    )
    alertas_atencion_min: float = Field(
        30, validation_alias=AliasChoices("ALERTAS_ATENCION_MIN", "SOLVENTA_ALERTAS_ATENCION_MIN")
    )
    alertas_falso_positivo_min: float = Field(
        5,
        validation_alias=AliasChoices(
            "ALERTAS_FALSO_POSITIVO_MIN", "SOLVENTA_ALERTAS_FALSO_POSITIVO_MIN"
        ),
    )

    @property
    def presupuesto_cotizar_ms(self) -> int:
        return self.latency_budget_ms.get(OPERACION_COTIZAR, PRESUPUESTO_COTIZAR_MS)

    @model_validator(mode="after")
    def _timeout_dentro_del_presupuesto(self) -> Self:
        # Si el timeout hacia cotizacion no es menor que el presupuesto, el servicio no arranca.
        presupuesto.por_operacion(self.latency_budget_ms)
        presupuesto.validar_timeouts(
            OPERACION_COTIZAR,
            self.presupuesto_cotizar_ms,
            {"cotizacion_timeout_ms": self.cotizacion_timeout_ms},
        )
        return self


settings = Settings()
