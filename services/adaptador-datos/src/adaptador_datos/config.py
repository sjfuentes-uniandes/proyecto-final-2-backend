from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SOLVENTA_")

    service_name: str = "adaptador-datos"
    database_url: str = "postgresql://solventa:solventa@localhost:5432/adaptador_datos"


settings = Settings()
