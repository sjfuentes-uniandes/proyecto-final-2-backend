"""Migraciones de la base de `siniestros` (HU-W19). No hay Alembic: SQL idempotente en
`migraciones/NNN_*.sql` (con su reversa en `migraciones/down/`), aplicado en orden al arrancar."""

from importlib import resources

import psycopg

_PAQUETE = resources.files("siniestros.adapters.outbound")
_LOCK = "SELECT pg_advisory_lock(hashtext('siniestros.migraciones'))"
_UNLOCK = "SELECT pg_advisory_unlock(hashtext('siniestros.migraciones'))"


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
