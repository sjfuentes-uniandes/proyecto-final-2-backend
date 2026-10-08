from solventa_common.settings import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "adaptador-datos"


settings = Settings()
