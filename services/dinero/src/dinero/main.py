from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from solventa_common.telemetry import instrumentar

from dinero.config import Settings

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool


def crear_app(
    settings: Settings | None = None, *, pool: "AsyncConnectionPool[Any] | None" = None
) -> FastAPI:
    """App de dinero. Las pruebas inyectan el pool sobre una base ya migrada; lo abren y lo cierran
    ellas."""
    settings = settings or Settings()

    @asynccontextmanager
    async def ciclo(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as pila:
            if pool is not None:
                app.state.pool = pool
            elif settings.db_host or settings.database_url_override:
                from psycopg_pool import AsyncConnectionPool

                from dinero.adapters.outbound.recaudos_pg import migrar

                migrar(settings.database_url)
                pool_base = AsyncConnectionPool(
                    settings.database_url,
                    min_size=1,
                    max_size=settings.db_pool_size,
                    open=False,
                )
                await pool_base.open()
                pila.push_async_callback(pool_base.close)
                app.state.pool = pool_base
            # HU-W16 (16.10): el consumidor de OfertaAceptada y el relay del outbox, sobre este pool.
            yield

    app = instrumentar(FastAPI(title=settings.service_name, lifespan=ciclo), settings)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = crear_app()
