from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from clientes.domain.identidad.modelos import (
    Cliente,
    DatosCliente,
    EstadoIdentidad,
    RegistroYaVinculado,
)

_COLUMNAS = (
    "id, sub, estado, verificado_en, registro_id, correo, tipo_documento, numero_documento, "
    "nombre_completo, fecha_nacimiento, proveedor, referencia"
)
_SELECT = f"SELECT {_COLUMNAS} FROM cliente"
_POR_ID = _SELECT + " WHERE id = %s"
_POR_SUB = _SELECT + " WHERE sub = %s"
_POR_REGISTRO = _SELECT + " WHERE registro_id = %s"
_POR_DOCUMENTO = _SELECT + " WHERE tipo_documento = %s AND numero_documento = %s"
# DO NOTHING + SELECT: dos solicitudes simultáneas del mismo `sub` terminan con un solo cliente.
_CREAR = "INSERT INTO cliente (id, sub, estado) VALUES (%s, %s, 'PENDIENTE') ON CONFLICT (sub) DO NOTHING"
_CREAR_REGISTRO = (
    "INSERT INTO cliente (id, registro_id, correo, estado) VALUES (%s, %s, %s, 'PENDIENTE') "
    "ON CONFLICT (registro_id) DO NOTHING"
)
# RETURNING solo devuelve fila si este INSERT creó el cliente: distingue el alta del reintento.
_CREAR_DOCUMENTO = (
    "INSERT INTO cliente (id, tipo_documento, numero_documento, nombre_completo, "
    "fecha_nacimiento, estado) VALUES (%s, %s, %s, %s, %s, 'PENDIENTE') "
    "ON CONFLICT (tipo_documento, numero_documento) DO NOTHING RETURNING id"
)
_ACTUALIZAR = (
    "UPDATE cliente SET estado = %s, verificado_en = %s, proveedor = %s, referencia = %s "
    "WHERE id = %s"
)
# Solo asocia si el cliente no tiene `sub` o ya es el mismo (idempotente).
_VINCULAR = (
    f"UPDATE cliente SET sub = %s WHERE id = %s AND (sub IS NULL OR sub = %s) RETURNING {_COLUMNAS}"
)


def migrar(url: str) -> None:
    archivos = sorted(
        (
            f
            for f in resources.files("clientes.adapters.outbound").joinpath("migraciones").iterdir()
            if f.name.endswith(".sql")
        ),
        key=lambda f: f.name,
    )
    with psycopg.connect(url, autocommit=True) as conexion:
        # Serializa las migraciones entre réplicas.
        conexion.execute("SELECT pg_advisory_lock(hashtext('clientes.migraciones'))")
        try:
            for archivo in archivos:
                conexion.execute(archivo.read_text(encoding="utf-8"))
        finally:
            conexion.execute("SELECT pg_advisory_unlock(hashtext('clientes.migraciones'))")


def _cliente(fila: dict[str, Any]) -> Cliente:
    return Cliente(
        id=fila["id"],
        sub=fila["sub"],
        estado=EstadoIdentidad(fila["estado"]),
        verificado_en=fila["verificado_en"],
        registro_id=fila["registro_id"],
        correo=fila["correo"],
        tipo_documento=fila["tipo_documento"],
        numero_documento=fila["numero_documento"],
        nombre_completo=fila["nombre_completo"],
        fecha_nacimiento=fila["fecha_nacimiento"],
        proveedor=fila["proveedor"],
        referencia=fila["referencia"],
    )


def crear_pool(
    url: str, minimo: int, maximo: int
) -> ConnectionPool[psycopg.Connection[dict[str, Any]]]:
    """Pool síncrono para las lecturas y escrituras de identidad (corren en asyncio.to_thread).

    Abrir una conexión TLS con RDS cuesta ~90 ms: sin pool, varias lecturas en paralelo (el nombre
    de cada cliente de una vista del portal) se encolaban y superaban el timeout de quien llama.
    """
    return ConnectionPool(
        url,
        min_size=minimo,
        max_size=maximo,
        kwargs={"row_factory": dict_row},
        check=ConnectionPool.check_connection,
        open=False,
    )


class RepositorioPostgres:
    """Sin `pool` abre una conexión por operación (migraciones, semilla y pruebas)."""

    def __init__(
        self, url: str, pool: ConnectionPool[psycopg.Connection[dict[str, Any]]] | None = None
    ) -> None:
        self.url = url
        self._pool = pool

    @contextmanager
    def _conectar(self) -> Iterator[psycopg.Connection[dict[str, Any]]]:
        # Las dos formas confirman al salir sin error y deshacen ante una excepción.
        if self._pool is not None:
            with self._pool.connection() as conexion:
                yield conexion
            return
        with psycopg.connect(self.url, row_factory=dict_row) as conexion:
            yield conexion

    def obtener(self, cliente_id: UUID) -> Cliente | None:
        with self._conectar() as conexion:
            fila = conexion.execute(_POR_ID, (cliente_id,)).fetchone()
        return _cliente(fila) if fila else None

    def por_sub(self, sub: str) -> Cliente | None:
        with self._conectar() as conexion:
            fila = conexion.execute(_POR_SUB, (sub,)).fetchone()
        return _cliente(fila) if fila else None

    def por_registro(self, registro_id: UUID) -> Cliente | None:
        with self._conectar() as conexion:
            fila = conexion.execute(_POR_REGISTRO, (registro_id,)).fetchone()
        return _cliente(fila) if fila else None

    def asegurar(self, sub: str) -> Cliente:
        with self._conectar() as conexion:
            conexion.execute(_CREAR, (uuid4(), sub))
            fila = conexion.execute(_POR_SUB, (sub,)).fetchone()
        if fila is None:
            raise RuntimeError("El cliente no existe tras crearlo")
        return _cliente(fila)

    def asegurar_registro(self, registro_id: UUID, correo: str) -> Cliente:
        with self._conectar() as conexion:
            conexion.execute(_CREAR_REGISTRO, (uuid4(), registro_id, correo))
            fila = conexion.execute(_POR_REGISTRO, (registro_id,)).fetchone()
        if fila is None:
            raise RuntimeError("El cliente no existe tras crearlo")
        return _cliente(fila)

    def asegurar_por_documento(self, datos: DatosCliente) -> tuple[Cliente, bool]:
        with self._conectar() as conexion:
            creado = (
                conexion.execute(
                    _CREAR_DOCUMENTO,
                    (
                        uuid4(),
                        datos.tipo_documento,
                        datos.numero_documento,
                        datos.nombre_completo,
                        datos.fecha_nacimiento,
                    ),
                ).fetchone()
                is not None
            )
            fila = conexion.execute(
                _POR_DOCUMENTO, (datos.tipo_documento, datos.numero_documento)
            ).fetchone()
        if fila is None:
            raise RuntimeError("El cliente no existe tras crearlo")
        return _cliente(fila), creado

    def vincular(self, cliente_id: UUID, sub: str) -> Cliente:
        try:
            with self._conectar() as conexion:
                fila = conexion.execute(_VINCULAR, (sub, cliente_id, sub)).fetchone()
        except psycopg.errors.UniqueViolation as error:
            raise RegistroYaVinculado() from error
        if fila is None:
            raise RegistroYaVinculado()
        return _cliente(fila)

    def guardar(self, cliente: Cliente) -> None:
        with self._conectar() as conexion:
            conexion.execute(
                _ACTUALIZAR,
                (
                    cliente.estado.value,
                    cliente.verificado_en,
                    cliente.proveedor,
                    cliente.referencia,
                    cliente.id,
                ),
            )
