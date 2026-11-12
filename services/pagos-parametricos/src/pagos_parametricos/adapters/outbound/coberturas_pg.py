"""La copia local de las coberturas paramétricas en PostgreSQL (HU-W21): `coberturas_activas` de
`migraciones/002_parametricos.sql`. pagos-parametricos es el único escritor de su base.

Todo corre en la transacción del inbox (`tx.conexion`): el bloqueo de la póliza, la lectura y la
escritura se confirman o se deshacen con el `eventId` del mensaje. Las candidatas de un evento salen
del índice `(tipo_evento, zona) WHERE estado = 'ACTIVA'` (DS3-19).
"""

from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from solventa_common.inbox import TransaccionInboxPg

from pagos_parametricos.domain.parametrico.modelos import Cobertura, EstadoCobertura, Regla

COLUMNAS = (
    "poliza_id, numero, estado, tipo_evento, zona, umbral, unidad, valor_pago, moneda, "
    "version_regla, vigente_desde, vigente_hasta, aggregate_version, ultimo_evento_en"
)
# Dos eventos de la misma póliza a la vez se aplican en serie (el bloqueo vive hasta el commit).
_BLOQUEAR = "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))"
_LEER = f"SELECT {COLUMNAS} FROM coberturas_activas WHERE poliza_id = %s"
_GUARDAR = (
    f"INSERT INTO coberturas_activas ({COLUMNAS}, actualizado_en) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()) "
    "ON CONFLICT (poliza_id) DO UPDATE SET numero = EXCLUDED.numero, estado = EXCLUDED.estado, "
    "tipo_evento = EXCLUDED.tipo_evento, zona = EXCLUDED.zona, umbral = EXCLUDED.umbral, "
    "unidad = EXCLUDED.unidad, valor_pago = EXCLUDED.valor_pago, moneda = EXCLUDED.moneda, "
    "version_regla = EXCLUDED.version_regla, vigente_desde = EXCLUDED.vigente_desde, "
    "vigente_hasta = EXCLUDED.vigente_hasta, aggregate_version = EXCLUDED.aggregate_version, "
    "ultimo_evento_en = EXCLUDED.ultimo_evento_en, actualizado_en = now()"
)
_CANDIDATAS = (
    f"SELECT {COLUMNAS} FROM coberturas_activas "
    "WHERE estado = 'ACTIVA' AND tipo_evento = %s AND zona = %s ORDER BY poliza_id"
)


def cobertura_de(fila: tuple[Any, ...]) -> Cobertura:
    (
        poliza_id,
        numero,
        estado,
        tipo_evento,
        zona,
        umbral,
        unidad,
        valor_pago,
        moneda,
        version_regla,
        desde,
        hasta,
        version,
        ultimo,
    ) = fila
    regla = (
        Regla(
            tipo_evento=tipo_evento,
            zona=zona,
            umbral=umbral,
            unidad=unidad,
            valor_pago=valor_pago,
            moneda=moneda,
            version_regla=version_regla,
            vigente_desde=desde,
            vigente_hasta=hasta,
        )
        if tipo_evento is not None
        else None
    )
    return Cobertura(
        poliza_id=poliza_id,
        estado=EstadoCobertura(estado),
        regla=regla,
        ultimo_evento_en=ultimo,
        aggregate_version=version,
        numero=numero,
    )


async def leer_candidatas(
    conexion: AsyncConnection[Any], tipo_evento: str, zona: str
) -> list[Cobertura]:
    cursor = await conexion.execute(_CANDIDATAS, (tipo_evento, zona))
    return [cobertura_de(fila) for fila in await cursor.fetchall()]


class CoberturasPg:
    """Implementa `RepositorioCoberturas[TransaccionInboxPg]`."""

    async def bloquear_y_leer(self, tx: TransaccionInboxPg, poliza_id: UUID) -> Cobertura | None:
        await tx.conexion.execute(_BLOQUEAR, (str(poliza_id),))
        cursor = await tx.conexion.execute(_LEER, (poliza_id,))
        fila = await cursor.fetchone()
        return cobertura_de(fila) if fila else None

    async def leer(self, tx: TransaccionInboxPg, poliza_id: UUID) -> Cobertura | None:
        cursor = await tx.conexion.execute(_LEER, (poliza_id,))
        fila = await cursor.fetchone()
        return cobertura_de(fila) if fila else None

    async def guardar(self, tx: TransaccionInboxPg, cobertura: Cobertura) -> None:
        regla = cobertura.regla
        await tx.conexion.execute(
            _GUARDAR,
            (
                cobertura.poliza_id,
                cobertura.numero,
                cobertura.estado.value,
                regla.tipo_evento if regla else None,
                regla.zona if regla else None,
                regla.umbral if regla else None,
                regla.unidad if regla else None,
                regla.valor_pago if regla else None,
                regla.moneda if regla else None,
                regla.version_regla if regla else None,
                regla.vigente_desde if regla else None,
                regla.vigente_hasta if regla else None,
                cobertura.aggregate_version,
                cobertura.ultimo_evento_en,
            ),
        )

    async def candidatas(
        self, tx: TransaccionInboxPg, tipo_evento: str, zona: str
    ) -> list[Cobertura]:
        return await leer_candidatas(tx.conexion, tipo_evento, zona)
