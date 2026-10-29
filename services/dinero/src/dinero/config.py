from solventa_common.settings import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "dinero"
    # Base (HU-W16, 16.9): la de ServiceSettings, con los nombres que infra/apps/ecs.tf entrega a
    # todo servicio con `database = true`: DB_HOST, DB_PORT, DB_NAME, DB_SSLMODE y DB_POOL_SIZE, y
    # DB_USER y DB_PASSWORD como secretos (o SOLVENTA_DATABASE_URL en local y pruebas). Sin base
    # configurada, la app arranca sin pool.


settings = Settings()
