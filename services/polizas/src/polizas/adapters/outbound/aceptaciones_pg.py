"""AceptacionesRepositorio en PostgreSQL (HU-W15, W15-B04). polizas es el único escritor de su base.

`guardar` inserta la aceptación y su fila OfertaAceptada en el outbox en una transacción
(regla 3). La unicidad por cotización y por Idempotency-Key resuelve las carreras: `ON CONFLICT DO
NOTHING` no inserta nada si otra solicitud ganó y el caso de uso relee la ganadora (DS2-11). Las
migraciones las aplica `decisiones_pg.migrar` (todos los .sql en orden).
"""

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from polizas.adapters.outbound import outbox_pg
from polizas.domain.aceptacion.evidencia import declaraciones_canonicas
from polizas.domain.aceptacion.modelos import Aceptacion, CanalAceptacion, Declaraciones
from polizas.domain.suscripcion.modelos import Decision

COLUMNAS = (
    "id",
    "cotizacion_id",
    "oferta_id",
    "decision_id",
    "cliente_id",
    "idempotency_key",
    "huella_solicitud",
    "huella_calculo",
    "vigente_hasta",
    "version_declaraciones",
    "declaraciones",
    "canal",
    "actor_id",
    "aceptada_en",
    "huella_evidencia",
    "correlation_id",
)
_INSERTAR = (
    f"INSERT INTO aceptaciones ({', '.join(COLUMNAS)}) "
    f"VALUES ({', '.join(['%s'] * len(COLUMNAS))}) ON CONFLICT DO NOTHING RETURNING id"
)
_SELECT = f"SELECT {', '.join(COLUMNAS)} FROM aceptaciones"
_POR_COTIZACION = _SELECT + " WHERE cotizacion_id = %s"
_POR_IDEMPOTENCIA = _SELECT + " WHERE idempotency_key = %s"


def fila_aceptacion(aceptacion: Aceptacion) -> dict[str, Any]:
    return {
        "id": aceptacion.id,
        "cotizacion_id": aceptacion.cotizacion_id,
        "oferta_id": aceptacion.oferta_id,
        "decision_id": aceptacion.decision_id,
        "cliente_id": aceptacion.cliente_id,
        "idempotency_key": aceptacion.idempotency_key,
        "huella_solicitud": aceptacion.huella_solicitud,
        "huella_calculo": aceptacion.huella_calculo,
        "vigente_hasta": aceptacion.vigente_hasta,
        "version_declaraciones": aceptacion.version_declaraciones,
        "declaraciones": Jsonb(declaraciones_canonicas(aceptacion.declaraciones)),
        "canal": aceptacion.canal.value,
        "actor_id": aceptacion.actor_id,
        "aceptada_en": aceptacion.aceptada_en,
        "huella_evidencia": aceptacion.huella_evidencia,
        "correlation_id": aceptacion.correlation_id,
    }


def aceptacion_de(fila: Mapping[str, Any]) -> Aceptacion:
    declaraciones = fila["declaraciones"]
    return Aceptacion(
        id=fila["id"],
        cotizacion_id=fila["cotizacion_id"],
        oferta_id=fila["oferta_id"],
        decision_id=fila["decision_id"],
        cliente_id=fila["cliente_id"],
        huella_calculo=fila["huella_calculo"],
        vigente_hasta=fila["vigente_hasta"],
        version_declaraciones=fila["version_declaraciones"],
        declaraciones=Declaraciones(
            leido_condiciones=declaraciones["leidoCondiciones"],
            informacion_veraz=declaraciones["informacionVeraz"],
            debito_automatico=declaraciones["debitoAutomatico"],
            avisos_electronicos=declaraciones["avisosElectronicos"],
        ),
        canal=CanalAceptacion(fila["canal"]),
        actor_id=fila["actor_id"],
        aceptada_en=fila["aceptada_en"],
        idempotency_key=fila["idempotency_key"],
        huella_solicitud=fila["huella_solicitud"],
        huella_evidencia=fila["huella_evidencia"],
        correlation_id=fila["correlation_id"],
    )


class AceptacionesPg:
    """Implementa AceptacionesRepositorio."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def guardar(self, aceptacion: Aceptacion, decision: Decision) -> bool:
        fila = fila_aceptacion(aceptacion)
        async with self._pool.connection() as conexion, conexion.transaction():
            cursor = await conexion.execute(_INSERTAR, [fila[c] for c in COLUMNAS])
            if await cursor.fetchone() is None:
                return False
            await outbox_pg.insertar(
                conexion, [outbox_pg.evento_de_aceptacion(aceptacion, decision)]
            )
        return True

    async def por_cotizacion(self, cotizacion_id: UUID) -> Aceptacion | None:
        return await self._una(_POR_COTIZACION, (cotizacion_id,))

    async def por_idempotencia(self, clave: UUID) -> Aceptacion | None:
        return await self._una(_POR_IDEMPOTENCIA, (clave,))

    async def _una(self, sql: str, parametros: Sequence[Any]) -> Aceptacion | None:
        async with self._pool.connection() as conexion:
            cursor = conexion.cursor(row_factory=dict_row)
            await cursor.execute(sql, parametros)
            fila = await cursor.fetchone()
        return None if fila is None else aceptacion_de(fila)
