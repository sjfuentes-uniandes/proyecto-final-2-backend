from pydantic import AliasChoices, Field
from solventa_common.settings import ServiceSettings


def _env(name: str) -> AliasChoices:
    return AliasChoices(name, f"SOLVENTA_{name}")


class Settings(ServiceSettings):
    service_name: str = "api-socios"

    aws_region: str = Field("us-east-1", validation_alias=_env("AWS_REGION"))
    partners_user_pool_id: str | None = Field(None, validation_alias=_env("PARTNERS_USER_POOL_ID"))
    partners_resource_server: str = Field(
        "solventa", validation_alias=_env("PARTNERS_RESOURCE_SERVER")
    )
    partners_usage_plans: dict[str, str] = Field(
        default_factory=dict, validation_alias=_env("PARTNERS_USAGE_PLANS")
    )
    # HU-W28: cada cuánto se publica PartnerQuotaUsage (0 = no se publica).
    quota_metric_interval_s: int = Field(0, ge=0, validation_alias=_env("QUOTA_METRIC_INTERVAL_S"))


settings = Settings()
