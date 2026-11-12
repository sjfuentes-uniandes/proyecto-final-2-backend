"""Migraciones de adaptador-telemetria: SQL idempotente de `migraciones/NNN_*.sql`, aplicado en
orden y serializado entre réplicas con un candado de sesión (el patrón de `dinero`, `polizas` y
`pagos-parametricos`; el backend no usa Alembic, CF-S3-01). Las reversas, para pruebas de
migración, están en `migraciones/down/`."""

from importlib import resources

import psycopg

_PAQUETE = resources.files("adaptador_telemetria.adapters.outbound")
_LOCK = "SELECT pg_advisory_lock(hashtext('adaptador_telemetria.migraciones'))"
_UNLOCK = "SELECT pg_advisory_unlock(hashtext('adaptador_telemetria.migraciones'))"


def migrar(url: str) -> None:
    """Aplica los .sql en orden. Idempotente y serializado entre réplicas."""
    archivos = sorted(
        (f for f in _PAQUETE.joinpath("migraciones").iterdir() if f.name.endswith(".sql")),
        key=lambda f: f.name,
    )
    with psycopg.connect(url, autocommit=True) as conexion:
        conexion.execute(_LOCK)
        try:
            for archivo in archivos:
                conexion.execute(archivo.read_text(encoding="utf-8"))
        finally:
            conexion.execute(_UNLOCK)
