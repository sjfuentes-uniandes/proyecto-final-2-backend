"""Medición de la verificación del consentimiento (HU-W31, PR 3.2; ADR 0011). Sin umbrales.

Mide por separado, sobre el mismo Postgres de Testcontainers y el mismo `AsyncConnectionPool`:

(a) lectura pura: la consulta de la verificación (`_VERIFICAR`, una captura del reloj) y la
    decisión del dominio, sin transacción explícita ni outbox;
(b) lectura + escritura: `ConsentimientosPg.verificar`, que además inserta
    `VerificacionConsentimientoRegistrada` en el outbox y confirma la transacción.

No es una prueba (pytest no la recoge) y no falla por rendimiento: imprime un informe. Se ejecuta
desde la raíz del repositorio, con Docker:

    uv run python services/clientes/tests/rendimiento/medir_verificacion.py
"""

import argparse
import asyncio
import os
import platform
import random
import statistics
import time
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

import psycopg
from clientes.adapters.outbound import consentimientos_pg
from clientes.adapters.outbound.consentimientos_pg import ConsentimientosPg, lectura_verificacion
from clientes.adapters.outbound.postgres import migrar
from clientes.domain.consentimientos.verificacion import SolicitudVerificacion, decidir
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

CUATRO = (
    "FA_PRODUCTOS_VIGENTES",
    "FA_HISTORIAL_PAGOS_12M",
    "FA_INGRESOS_AGREGADOS",
    "DA_REGISTROS_PUBLICOS",
)
SOLICITUD = SolicitudVerificacion.crear("PERFILAMIENTO_PRECIO", CUATRO)


def _sembrar(url: str, clientes: int) -> list[UUID]:
    """Cada cliente: un ACTIVO vigente y dos revocados anteriores (historial realista)."""
    ids = [uuid4() for _ in range(clientes)]
    with psycopg.connect(url, autocommit=True) as conexion:
        with conexion.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO cliente (id, sub, estado) VALUES (%s, %s, 'VERIFICADO')",
                [(i, f"sub-{i}") for i in ids],
            )
            filas = []
            for i in ids:
                for estado, hace in (("REVOCADO", 700), ("REVOCADO", 400), ("ACTIVO", 30)):
                    filas.append((uuid4(), i, estado, f"{hace} days", estado))
            cursor.executemany(
                "WITH t AS MATERIALIZED (SELECT clock_timestamp() AS ahora) "
                "INSERT INTO consentimiento (id, cliente_id, proposito, alcances, version_texto, "
                "idioma, estado, otorgado_en, vigente_hasta, revocado_en) "
                "SELECT %s, %s, 'PERFILAMIENTO_PRECIO', "
                "ARRAY['FA_PRODUCTOS_VIGENTES','DA_REGISTROS_PUBLICOS'], 'v1', 'es-CO', %s, "
                "t.ahora - %s::interval, t.ahora - %s::interval + interval '365 days', "
                "CASE WHEN %s = 'REVOCADO' THEN t.ahora - interval '360 days' END FROM t",
                [(a, b, c, d, d, e) for a, b, c, d, e in filas],
            )
        conexion.execute("ANALYZE")
    return ids


async def _lectura(pool: AsyncConnectionPool, cliente_id: UUID) -> None:
    async with pool.connection() as conexion:
        cursor = conexion.cursor(row_factory=dict_row)
        await cursor.execute(
            consentimientos_pg._VERIFICAR,
            {"cliente": cliente_id, "proposito": SOLICITUD.proposito},
        )
        fila = await cursor.fetchone()
        assert fila is not None
        decidir(SOLICITUD, lectura_verificacion(fila))


async def _correr(
    operacion: Callable[[UUID], Awaitable[Any]],
    ids: list[UUID],
    llamadas: int,
    concurrencia: int,
) -> tuple[list[float], float]:
    """Latencias en ms de cada llamada y duración total en s."""
    latencias: list[float] = []
    semaforo = asyncio.Semaphore(concurrencia)

    async def una() -> None:
        async with semaforo:
            inicio = time.perf_counter_ns()
            await operacion(random.choice(ids))
            latencias.append((time.perf_counter_ns() - inicio) / 1e6)

    inicio = time.perf_counter()
    await asyncio.gather(*(una() for _ in range(llamadas)))
    return latencias, time.perf_counter() - inicio


def _percentil(valores: list[float], p: float) -> float:
    ordenados = sorted(valores)
    return ordenados[min(len(ordenados) - 1, round(p / 100 * (len(ordenados) - 1)))]


def _fila(nombre: str, latencias: list[float], total: float) -> str:
    return (
        f"| {nombre} | {len(latencias)} | {statistics.mean(latencias):.2f} | "
        f"{_percentil(latencias, 50):.2f} | {_percentil(latencias, 95):.2f} | "
        f"{_percentil(latencias, 99):.2f} | {max(latencias):.2f} | {len(latencias) / total:.0f} |"
    )


async def medir(url: str, args: argparse.Namespace) -> None:
    ids = _sembrar(url, args.clientes)
    async with AsyncConnectionPool(url, min_size=args.pool, max_size=args.pool, open=False) as pool:
        repo = ConsentimientosPg(pool)

        async def lectura(cliente_id: UUID) -> None:
            await _lectura(pool, cliente_id)

        async def escritura(cliente_id: UUID) -> None:
            await repo.verificar(cliente_id, SOLICITUD, "corr-medicion")

        filas = []
        for concurrencia, llamadas in ((1, args.secuenciales), (args.concurrencia, args.llamadas)):
            for nombre, operacion in (
                ("(a) lectura", lectura),
                ("(b) lectura + outbox", escritura),
            ):
                await _correr(operacion, ids, args.calentamiento, concurrencia)
                latencias, total = await _correr(operacion, ids, llamadas, concurrencia)
                filas.append(_fila(f"{nombre}, concurrencia {concurrencia}", latencias, total))
        async with pool.connection() as conexion:
            cursor = await conexion.execute("SELECT count(*) FROM outbox")
            outbox = (await cursor.fetchone() or (0,))[0]
            cursor = await conexion.execute("SHOW server_version")
            version = (await cursor.fetchone() or ("?",))[0]

    print(f"Postgres {version}; Python {platform.python_version()}; {platform.platform()}")
    print(f"CPU: {platform.processor() or '?'} ({os.cpu_count()} hilos lógicos)")
    print(
        f"{args.clientes} clientes, {3 * args.clientes} consentimientos; pool {args.pool}; "
        f"calentamiento {args.calentamiento} por serie; filas de outbox al final: {outbox}"
    )
    print()
    print("| Serie | Llamadas | Media ms | p50 ms | p95 ms | p99 ms | Máx ms | Llamadas/s |")
    print("|---|---|---|---|---|---|---|---|")
    print("\n".join(filas))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--clientes", type=int, default=10_000)
    parser.add_argument("--secuenciales", type=int, default=3_000)
    parser.add_argument("--llamadas", type=int, default=20_000)
    parser.add_argument("--concurrencia", type=int, default=16)
    parser.add_argument("--pool", type=int, default=10)
    parser.add_argument("--calentamiento", type=int, default=300)
    args = parser.parse_args()
    random.seed(31)
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("postgres:16", driver=None) as contenedor:
        url = contenedor.get_connection_url()
        migrar(url)
        asyncio.run(medir(url, args), loop_factory=asyncio.SelectorEventLoop)


if __name__ == "__main__":
    main()
