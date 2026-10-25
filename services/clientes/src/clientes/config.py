from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import SettingsConfigDict
from solventa_common.settings import ServiceSettings
from solventa_common.token_registro import SECRETO_LOCAL

# Los clientes sintéticos nunca se siembran en un ambiente con datos reales.
AMBIENTES_CON_SEMILLA = frozenset({"int", "local"})


def _env(nombre: str) -> AliasChoices:
    # Acepta el nombre que entrega la infraestructura y su variante SOLVENTA_ (A6).
    return AliasChoices(nombre, f"SOLVENTA_{nombre}")


class Settings(ServiceSettings):
    model_config = SettingsConfigDict(
        env_prefix="SOLVENTA_", extra="ignore", hide_input_in_errors=True
    )

    service_name: str = "clientes"

    # URL que entrega la infraestructura por cada servicio que invoca (infra/apps/ecs.tf).
    adaptador_identidad_url: str = Field(
        "http://localhost:8087", validation_alias=_env("ADAPTADOR_IDENTIDAD_URL")
    )
    # Debe superar el timeout del proveedor KYC en adaptador-identidad (3 s): si no, `clientes`
    # se rinde antes que el adaptador y una verificación lenta pero válida se pierde.
    adaptador_identidad_timeout_ms: int = Field(4000, ge=1)
    # HU-W05: tope de GET /v1/clientes/{id}/identidad (recorrido de cotización); con
    # X-Deadline-Ms corta en min(este, 700, deadline − 20) y responde 504 TIEMPO_AGOTADO.
    consulta_timeout_ms: int = Field(500, ge=1)

    # Secreto con el que se firma el token de registro; lo valida el Lambda pre sign-up.
    registro_token_secret: SecretStr | None = Field(
        None, validation_alias=_env("REGISTRO_TOKEN_SECRET"), repr=False
    )
    # El primer ingreso ocurre tras confirmar el correo: 24 h, lo que dura el código de Cognito.
    registro_vinculacion_gracia_s: int = Field(86_400, ge=0)

    # Conexiones mínimas del pool de consentimientos; el máximo es `db_pool_size` (DB_POOL_SIZE).
    db_pool_min_size: int = Field(1, ge=1, validation_alias=_env("DB_POOL_MIN_SIZE"))

    # Siembra C01–C19 al arrancar (DEF-01). Solo la activa infra/envs/int/apps.tfvars.
    semilla_sintetica: bool = Field(False, validation_alias=_env("CLIENTES_SEMILLA_SINTETICA"))

    @model_validator(mode="after")
    def _semilla_solo_en_pruebas(self) -> "Settings":
        if self.semilla_sintetica and self.environment not in AMBIENTES_CON_SEMILLA:
            raise ValueError("CLIENTES_SEMILLA_SINTETICA solo se permite en int o local")
        return self

    @model_validator(mode="after")
    def _tamano_del_pool(self) -> "Settings":
        if self.db_pool_size < self.db_pool_min_size:
            raise ValueError("DB_POOL_SIZE no puede ser menor que DB_POOL_MIN_SIZE")
        return self

    @model_validator(mode="after")
    def _secreto_de_registro(self) -> "Settings":
        if self.registro_token_secret is None:
            if self.environment != "local":
                raise ValueError("REGISTRO_TOKEN_SECRET es obligatorio fuera de local")
            self.registro_token_secret = SecretStr(SECRETO_LOCAL)
        return self

    @property
    def secreto_registro(self) -> str:
        assert self.registro_token_secret is not None
        return self.registro_token_secret.get_secret_value()


settings = Settings()
