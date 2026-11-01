"""LecturaEstadoPolizas en PostgreSQL (HU-W18): estado y listado sobre `poliza_cliente`.

Todo valor viaja como parámetro; el SQL solo se arma con fragmentos constantes. El filtro de
`estado` se evalúa aquí con `hoy` y `dias_por_vencer`, con las mismas reglas de `estado_de`.
"""

from typing import TYPE_CHECKING, Any
from uuid import UUID

from psycopg.rows import tuple_row

from consulta.adapters.outbound.lectura_pg import poliza_de
from consulta.adapters.outbound.polizas_pg import COLUMNAS
from consulta.application.estado.puertos import FiltroPolizas, ResultadoBusqueda
from consulta.domain.polizas.estado import EstadoVigencia
from consulta.domain.polizas.modelos import PolizaProyectada

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

_EN_VIGENCIA = "vigencia_desde <= %(hoy)s AND vigencia_hasta >= %(hoy)s"
_RESTANTES = "(vigencia_hasta - %(hoy)s::date)"
_CONDICION_ESTADO = {
    EstadoVigencia.NO_INICIADA: "vigencia_desde > %(hoy)s",
    EstadoVigencia.VENCIDA: "vigencia_hasta < %(hoy)s",
    EstadoVigencia.POR_VENCER: f"{_EN_VIGENCIA} AND {_RESTANTES} <= %(dias)s",
    EstadoVigencia.VIGENTE: f"{_EN_VIGENCIA} AND {_RESTANTES} > %(dias)s",
}

_SELECT = f"SELECT {', '.join(COLUMNAS)} FROM poliza_cliente"
_POR_ID = f"{_SELECT} WHERE poliza_id = %s"
_PAGINA = " ORDER BY emitida_en DESC, poliza_id LIMIT %(limite)s OFFSET %(desplazamiento)s"


def _donde(filtro: FiltroPolizas) -> tuple[str, dict[str, Any]]:
    condiciones: list[str] = []
    parametros: dict[str, Any] = {"hoy": filtro.hoy, "dias": filtro.dias_por_vencer}
    if filtro.numero is not None:
        condiciones.append("numero = %(numero)s")
        parametros["numero"] = filtro.numero
    if filtro.ramo is not None:
        condiciones.append("ramo = %(ramo)s")
        parametros["ramo"] = filtro.ramo
    if filtro.estado is not None:
        condiciones.append(_CONDICION_ESTADO[filtro.estado])
    if filtro.vigencia_desde is not None:
        condiciones.append("vigencia_hasta >= %(vigencia_desde)s")
        parametros["vigencia_desde"] = filtro.vigencia_desde
    return (" WHERE " + " AND ".join(condiciones) if condiciones else ""), parametros


class LecturaEstadoPolizasPg:
    """Implementa LecturaEstadoPolizas sobre el pool."""

    def __init__(self, pool: "AsyncConnectionPool[Any]") -> None:
        self._pool = pool

    async def por_id(self, poliza_id: UUID) -> PolizaProyectada | None:
        async with self._pool.connection() as conexion:
            cursor = await conexion.cursor(row_factory=tuple_row).execute(_POR_ID, (poliza_id,))
            fila = await cursor.fetchone()
        return poliza_de(fila) if fila else None

    async def buscar(self, filtro: FiltroPolizas, pagina: int, tamano: int) -> ResultadoBusqueda:
        donde, parametros = _donde(filtro)
        # `hoy` y `dias` sobran si el filtro no los usa; psycopg ignora los parámetros nombrados
        # que no aparecen en la consulta.
        total_sql = (
            f"SELECT (SELECT count(*) FROM poliza_cliente{donde}), "
            "(SELECT max(proyectada_en) FROM poliza_cliente)"
        )
        pagina_sql = _SELECT + donde + _PAGINA
        desplazamiento = {"limite": tamano, "desplazamiento": (pagina - 1) * tamano}
        async with self._pool.connection() as conexion:
            cursor = conexion.cursor(row_factory=tuple_row)
            resumen = await (await cursor.execute(total_sql, parametros)).fetchone()
            filas = await (await cursor.execute(pagina_sql, parametros | desplazamiento)).fetchall()
        total, actualizado_en = resumen if resumen else (0, None)
        return ResultadoBusqueda(tuple(poliza_de(f) for f in filas), total, actualizado_en)
