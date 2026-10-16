from importlib import resources
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from clientes.domain.identidad.modelos import Cliente, EstadoIdentidad, RegistroYaVinculado

_COLUMNAS = "id, sub, estado, verificado_en, registro_id, correo"
_SELECT = f"SELECT {_COLUMNAS} FROM cliente"
_POR_ID = _SELECT + " WHERE id = %s"
_POR_SUB = _SELECT + " WHERE sub = %s"
_POR_REGISTRO = _SELECT + " WHERE registro_id = %s"
# DO NOTHING + SELECT: dos solicitudes simultáneas del mismo `sub` terminan con un solo cliente.
_CREAR = "INSERT INTO cliente (id, sub, estado) VALUES (%s, %s, 'PENDIENTE') ON CONFLICT (sub) DO NOTHING"
_CREAR_REGISTRO = (
    "INSERT INTO cliente (id, registro_id, correo, estado) VALUES (%s, %s, %s, 'PENDIENTE') "
    "ON CONFLICT (registro_id) DO NOTHING"
)
_ACTUALIZAR = "UPDATE cliente SET estado = %s, verificado_en = %s WHERE id = %s"
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
    )


class RepositorioPostgres:
    def __init__(self, url: str) -> None:
        self.url = url

    def _conectar(self) -> psycopg.Connection[dict[str, Any]]:
        return psycopg.connect(self.url, row_factory=dict_row)

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
            conexion.execute(_ACTUALIZAR, (cliente.estado.value, cliente.verificado_en, cliente.id))
