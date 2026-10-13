"""Repositorio de socios en PostgreSQL (base api_socios)."""

from importlib import resources
from typing import Any

import psycopg
from psycopg.rows import dict_row

from api_socios.domain.socios import CanalVenta, Estado, Permiso, Plan, Socio, identificador

# Consultas literales completas: los valores siempre van como parámetros de psycopg.
_SELECT = "SELECT id, nombre, canal, plan, permisos, estado, client_id, api_key_id, creado, desactivado FROM socio"
_POR_ID = _SELECT + " WHERE id = %s"
_POR_CLIENT_ID = _SELECT + " WHERE client_id = %s"
_TODOS = _SELECT + " ORDER BY id"
_GUARDAR = (
    "INSERT INTO socio (id, nombre, canal, plan, permisos, estado, client_id, api_key_id, creado, desactivado) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (id) DO UPDATE SET estado = EXCLUDED.estado, desactivado = EXCLUDED.desactivado"
)


def migrar(url: str) -> None:
    """Aplica en orden las migraciones SQL del paquete; cada una es idempotente."""
    archivos = sorted(
        (f for f in resources.files("api_socios.adapters.outbound").joinpath("migraciones").iterdir()
         if f.name.endswith(".sql")),
        key=lambda f: f.name,
    )
    with psycopg.connect(url, autocommit=True) as conexion:
        # Varias réplicas pueden arrancar a la vez: un candado serializa las migraciones.
        conexion.execute("SELECT pg_advisory_lock(hashtext('api_socios.migraciones'))")
        try:
            for archivo in archivos:
                conexion.execute(archivo.read_text(encoding="utf-8"))
        finally:
            conexion.execute("SELECT pg_advisory_unlock(hashtext('api_socios.migraciones'))")


def _socio(fila: dict[str, Any]) -> Socio:
    return Socio(
        id=fila["id"], nombre=fila["nombre"], canal=CanalVenta(fila["canal"]), plan=Plan(fila["plan"]),
        permisos=frozenset(Permiso(p) for p in fila["permisos"]), estado=Estado(fila["estado"]),
        client_id=fila["client_id"], api_key_id=fila["api_key_id"], creado=fila["creado"],
        desactivado=fila["desactivado"],
    )


class RepositorioPostgres:
    def __init__(self, url: str) -> None:
        self.url = url

    def _conectar(self) -> psycopg.Connection[dict[str, Any]]:
        return psycopg.connect(self.url, row_factory=dict_row)

    def siguiente_identificador(self) -> str:
        with self._conectar() as conexion:
            fila = conexion.execute("SELECT nextval('socio_secuencia') AS n").fetchone()
        if fila is None:
            raise RuntimeError("La secuencia de socios no devolvió valor")
        return identificador(int(fila["n"]))

    def guardar(self, socio: Socio) -> None:
        with self._conectar() as conexion:
            conexion.execute(
                _GUARDAR,
                (socio.id, socio.nombre, socio.canal.value, socio.plan.value, sorted(p.value for p in socio.permisos),
                 socio.estado.value, socio.client_id, socio.api_key_id, socio.creado, socio.desactivado),
            )

    def obtener(self, socio_id: str) -> Socio | None:
        with self._conectar() as conexion:
            fila = conexion.execute(_POR_ID, (socio_id,)).fetchone()
        return _socio(fila) if fila else None

    def por_client_id(self, client_id: str) -> Socio | None:
        with self._conectar() as conexion:
            fila = conexion.execute(_POR_CLIENT_ID, (client_id,)).fetchone()
        return _socio(fila) if fila else None

    def listar(self) -> list[Socio]:
        with self._conectar() as conexion:
            filas = conexion.execute(_TODOS).fetchall()
        return [_socio(f) for f in filas]
