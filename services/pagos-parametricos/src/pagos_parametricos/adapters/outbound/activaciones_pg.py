"""Las activaciones `PENDIENTE` de W21 y su conversión en siniestros (HU-W22), en PostgreSQL.

Tablas de `migraciones/002_parametricos.sql` y `003_activacion_siniestro.sql`. Cada método toma una
conexión del pool **solo mientras dura su consulta**: la continuación del consumidor espera a
`siniestros` sin conexión retenida (§13, regla 8).
"""

from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from pagos_parametricos.domain.parametrico.activacion import (
    ESTADO_PENDIENTE,
    ESTADO_SINIESTRO_CREADO,
    ActivacionPendiente,
    Disparador,
    SiniestroCreado,
)

_PENDIENTES = (
    "SELECT a.evento_id, a.poliza_id, a.valor_pago, a.moneda, a.version_regla, c.cliente_id, "
    "c.numero, c.ramo, c.umbral, c.vigente_desde, c.vigente_hasta "
    "FROM activaciones a LEFT JOIN coberturas_activas c USING (poliza_id) "
    f"WHERE a.evento_id = %s AND a.estado = '{ESTADO_PENDIENTE}' ORDER BY a.poliza_id"
)
# Solo una activación todavía `PENDIENTE` cambia: dos réplicas que reciben el mismo mensaje dejan la
# primera respuesta (ambas son el mismo siniestro, por la clave de idempotencia).
_CREADA = (
    f"UPDATE activaciones SET estado = '{ESTADO_SINIESTRO_CREADO}', siniestro_id = %s, "
    "siniestro_codigo = %s, disparador = %s "
    f"WHERE evento_id = %s AND poliza_id = %s AND estado = '{ESTADO_PENDIENTE}'"
)
_DUPLICADO = "UPDATE eventos_evaluados SET duplicados = duplicados + 1 WHERE evento_id = %s"


def activacion_de(fila: tuple[Any, ...]) -> ActivacionPendiente:
    (
        evento_id,
        poliza_id,
        valor_pago,
        moneda,
        version_regla,
        cliente_id,
        numero,
        ramo,
        umbral,
        desde,
        hasta,
    ) = fila
    return ActivacionPendiente(
        evento_id=evento_id,
        poliza_id=poliza_id,
        valor_pago=valor_pago,
        moneda=moneda.strip(),
        version_regla=version_regla,
        cliente_id=cliente_id,
        numero=numero,
        ramo=ramo,
        umbral=umbral,
        vigente_desde=desde,
        vigente_hasta=hasta,
    )


def disparador_a_json(disparador: Disparador) -> dict[str, str]:
    return {
        "tipoEvento": disparador.tipo_evento,
        "valorObservado": disparador.valor_observado,
        "umbral": disparador.umbral,
        "unidad": disparador.unidad,
    }


class ActivacionesPg:
    """Implementa `RepositorioActivaciones` sobre el pool del servicio."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def pendientes(self, evento_id: UUID) -> list[ActivacionPendiente]:
        async with self._pool.connection() as conexion:
            cursor = await conexion.execute(_PENDIENTES, (evento_id,))
            return [activacion_de(fila) for fila in await cursor.fetchall()]

    async def marcar_creada(
        self,
        evento_id: UUID,
        poliza_id: UUID,
        siniestro: SiniestroCreado,
        disparador: Disparador,
    ) -> None:
        async with self._pool.connection() as conexion, conexion.transaction():
            await conexion.execute(
                _CREADA,
                (
                    siniestro.siniestro_id,
                    siniestro.codigo,
                    Jsonb(disparador_a_json(disparador)),
                    evento_id,
                    poliza_id,
                ),
            )

    async def sumar_duplicado(self, evento_id: UUID) -> None:
        async with self._pool.connection() as conexion, conexion.transaction():
            await conexion.execute(_DUPLICADO, (evento_id,))
