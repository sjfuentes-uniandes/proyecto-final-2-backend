from typing import Self

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import SettingsConfigDict
from solventa_common import presupuesto
from solventa_common.settings import ServiceSettings

# HU-W05: presupuesto de socio.cotizar si LATENCY_BUDGET_MS no lo trae (latency_budget_ms de
# infra/envs/<env>/apps.tfvars). Debe superar el timeout hacia cotizacion.
PRESUPUESTO_COTIZAR_MS = 2000
OPERACION_COTIZAR = "socio.cotizar"


def _env(name: str) -> AliasChoices:
    return AliasChoices(name, f"SOLVENTA_{name}")


class Settings(ServiceSettings):
    # Un error de validación al arrancar no debe imprimir la configuración (DB_PASSWORD).
    model_config = SettingsConfigDict(
        env_prefix="SOLVENTA_", extra="ignore", hide_input_in_errors=True
    )

    service_name: str = "api-socios"

    aws_region: str = Field("us-east-1", validation_alias=_env("AWS_REGION"))
    partners_user_pool_id: str | None = Field(None, validation_alias=_env("PARTNERS_USER_POOL_ID"))
    partners_resource_server: str = Field(
        "solventa", validation_alias=_env("PARTNERS_RESOURCE_SERVER")
    )
    partners_usage_plans: dict[str, str] = Field(
        default_factory=dict, validation_alias=_env("PARTNERS_USAGE_PLANS")
    )
    # HU-W04: la oferta del socio la calcula cotizacion (catálogo de infra: calls = ["cotizacion"],
    # que define COTIZACION_URL). El timeout es el de bff-web hacia cotizacion.
    cotizacion_url: str = Field("http://localhost:8085", validation_alias=_env("COTIZACION_URL"))
    cotizacion_timeout_ms: int = Field(1500, ge=1)
    # HU-W28: cada cuánto se publica PartnerQuotaUsage (0 = no se publica).
    quota_metric_interval_s: int = Field(0, ge=0, validation_alias=_env("QUOTA_METRIC_INTERVAL_S"))
    # HU-W05: presupuesto por operación (JSON). "socio.cotizar" inicia el X-Deadline-Ms que
    # recibe cotizacion: el restante del socio acota el presupuesto del recorrido.
    latency_budget_ms: dict[str, int] = Field(
        default_factory=dict, validation_alias=_env("LATENCY_BUDGET_MS")
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
