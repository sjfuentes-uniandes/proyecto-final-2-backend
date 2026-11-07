"""Regla de `aggregateVersion` para proyecciones que reciben eventos sin orden (HU-W24).

La cola de cada grupo de consumidores no garantiza orden (D2: SQS estándar, sin FIFO). Una copia
local que se alimenta de los eventos de un agregado (por ejemplo `coberturas_activas` de
`pagos-parametricos`, HU-W21) solo **avanza** con un `aggregateVersion` mayor que el que ya tiene:
una `PolizaCancelada` (versión 2) que llega antes que la `PolizaEmitida` (versión 1) deja la
cobertura cancelada, y la emisión atrasada ya no la reactiva. Con el Inbox por `eventId`, el estado
final es el mismo que con una entrega única y ordenada (AC4). Respaldo: la nota de
`modelo_concurrencia.puml`, "La entrega puede repetirse; el efecto del negocio no".

- `avanza(actual, nueva)`: la regla, para el dominio.
- `leer_version(evento)`: el `aggregateVersion` del sobre, entero positivo; si falta o no es válido,
  `EventoInvalido` (el mensaje es envenenado y termina en la DLQ: sin versión no hay forma de ordenar).
- `upsert_por_version(...)`: la misma regla en un `INSERT … ON CONFLICT DO UPDATE … WHERE`, para
  escribirla en la transacción del inbox sin leer antes. Dos consumidores con versiones distintas
  del mismo agregado a la vez: el `ON CONFLICT` del segundo espera al commit del primero y compara
  contra la versión ya confirmada.

La tabla guarda la versión en una columna entera (`aggregate_version` por omisión). Este módulo no
define el contenido de la proyección: eso es del servicio.
"""

from collections.abc import Sequence
from typing import Any

from psycopg import AsyncConnection, sql

from solventa_common.inbox import EventoInvalido, EventoRecibido

COLUMNA_VERSION = "aggregate_version"
CAMPO_VERSION = "aggregateVersion"


def avanza(actual: int | None, nueva: int) -> bool:
    """True si `nueva` debe reemplazar a `actual` (sin estado previo, siempre)."""
    return actual is None or nueva > actual


def leer_version(evento: EventoRecibido) -> int:
    """El `aggregateVersion` del sobre. `bool` no cuenta como entero."""
    version = evento.sobre.get(CAMPO_VERSION)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise EventoInvalido
    return version


def upsert_por_version(
    tabla: str,
    clave: Sequence[str],
    columnas: Sequence[str],
    *,
    columna_version: str = COLUMNA_VERSION,
) -> sql.Composed:
    """`INSERT` de `clave + columnas + versión` que, si la fila existe, solo la reemplaza con una
    versión mayor. Devuelve una fila (`RETURNING`) solo si insertó o actualizó.

    Los parámetros van en el orden `clave`, `columnas` y la versión al final.
    """
    if not clave:
        raise ValueError("la clave no puede estar vacía")
    if columna_version in clave or columna_version in columnas:
        raise ValueError("la versión va aparte de la clave y las columnas")
    todas = [*clave, *columnas, columna_version]
    identificador = sql.Identifier(tabla)
    actualizar = [*columnas, columna_version]
    return sql.SQL(
        "INSERT INTO {tabla} ({todas}) VALUES ({valores}) "
        "ON CONFLICT ({clave}) DO UPDATE SET {asignaciones} "
        "WHERE {tabla}.{version} < EXCLUDED.{version} "
        "RETURNING {version}"
    ).format(
        tabla=identificador,
        todas=sql.SQL(", ").join(map(sql.Identifier, todas)),
        valores=sql.SQL(", ").join(sql.Placeholder() for _ in todas),
        clave=sql.SQL(", ").join(map(sql.Identifier, clave)),
        asignaciones=sql.SQL(", ").join(
            sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(columna))
            for columna in actualizar
        ),
        version=sql.Identifier(columna_version),
    )


async def aplicar_por_version(
    conexion: AsyncConnection[Any],
    sentencia: sql.Composed,
    clave: Sequence[Any],
    valores: Sequence[Any],
    version: int,
) -> bool:
    """Ejecuta `upsert_por_version` en la transacción del inbox; True si la proyección avanzó.

    False es un evento atrasado: se confirma igual (el `eventId` queda en el inbox) y se borra.
    """
    cursor = await conexion.execute(sentencia, (*clave, *valores, version))
    return await cursor.fetchone() is not None
