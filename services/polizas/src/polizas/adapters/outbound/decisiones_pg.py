"""DecisionesRepositorio en PostgreSQL (HU-W13). polizas es el único escritor de su base.

`guardar` inserta la decisión y su fila de outbox en una transacción (regla 3). La unicidad por
cotización, por Idempotency-Key y por código resuelve las carreras: `ON CONFLICT DO NOTHING` no
inserta nada si otra solicitud ganó, y el caso de uso relee la decisión ganadora (DS2-05). Toma una
conexión del pool solo durante cada operación: nunca mientras se espera a otro servicio (regla 8).
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from importlib import resources
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from polizas.adapters.outbound import outbox_pg
from polizas.domain.suscripcion.insumos import insumos_canonicos, insumos_de
from polizas.domain.suscripcion.modelos import (
    CasoBandeja,
    Decision,
    Evaluacion,
    FiltrosBandeja,
    Regla,
    ReglaEvaluada,
    ResultadoDecision,
    ResultadoRegla,
    TipoDecision,
    TipoRegla,
    Visibilidad,
)

_PAQUETE = resources.files("polizas.adapters.outbound")
_LOCK = "SELECT pg_advisory_lock(hashtext('polizas.migraciones'))"
_UNLOCK = "SELECT pg_advisory_unlock(hashtext('polizas.migraciones'))"
REVISION = ResultadoDecision.REVISION_REQUERIDA.value

COLUMNAS = (
    "id",
    "codigo",
    "cotizacion_id",
    "oferta_id",
    "cotizacion_codigo",
    "cliente_id",
    "producto_id",
    "idempotency_key",
    "huella_solicitud",
    "resultado",
    "tipo",
    "regla_determinante",
    "reglas_evaluadas",
    "motivos",
    "insumos",
    "hash_insumos",
    "version_reglas",
    "hash_reglas",
    "decidida_en",
    "duracion_ms",
    "correlation_id",
    "actor_id",
)
_INSERTAR = (
    f"INSERT INTO decisiones_suscripcion ({', '.join(COLUMNAS)}) "
    f"VALUES ({', '.join(['%s'] * len(COLUMNAS))}) ON CONFLICT DO NOTHING RETURNING id"
)
_SELECT = f"SELECT {', '.join(COLUMNAS)} FROM decisiones_suscripcion"
_POR_ID = _SELECT + " WHERE id = %s"
_POR_COTIZACION = _SELECT + " WHERE cotizacion_id = %s"
_POR_IDEMPOTENCIA = _SELECT + " WHERE idempotency_key = %s"
_COLUMNAS_CASO = (
    "id, codigo, cotizacion_id, cotizacion_codigo, cliente_id, producto_id, "
    "insumos->>'moneda' AS moneda, insumos->>'sumaAsegurada' AS suma_asegurada, motivos, "
    "reglas_evaluadas, decidida_en"
)
_CONTAR_DIA = (
    f"SELECT count(*) FILTER (WHERE resultado <> '{REVISION}'), "
    f"count(*) FILTER (WHERE resultado = '{REVISION}'), avg(duracion_ms) "
    "FROM decisiones_suscripcion WHERE decidida_en >= %s AND decidida_en < %s"
)
_CONTAR_FUERA = (
    f"SELECT count(*) FROM decisiones_suscripcion WHERE resultado = '{REVISION}' "
    "AND decidida_en < %s"
)


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


# --- Decisión ↔ fila ------------------------------------------------------------------------


def _regla_evaluada(evaluada: ReglaEvaluada) -> dict[str, Any]:
    regla = evaluada.regla
    return {
        "codigo": regla.codigo,
        "orden": regla.orden,
        "tipo": regla.tipo.value,
        "descripcion": regla.descripcion,
        "campo": regla.campo,
        "operador": regla.operador,
        "valor": regla.valor if isinstance(regla.valor, str) else list(regla.valor),
        "visibilidad": regla.visibilidad.value,
        "motivoCliente": regla.motivo_cliente,
        "valorCaso": evaluada.valor_caso,
        "resultado": evaluada.resultado.value,
    }


def _de_regla_evaluada(datos: Mapping[str, Any]) -> ReglaEvaluada:
    valor = datos["valor"]
    regla = Regla(
        codigo=datos["codigo"],
        orden=datos["orden"],
        tipo=TipoRegla(datos["tipo"]),
        descripcion=datos["descripcion"],
        campo=datos["campo"],
        operador=datos["operador"],
        valor=valor if isinstance(valor, str) else tuple(valor),
        visibilidad=Visibilidad(datos["visibilidad"]),
        motivo_cliente=datos["motivoCliente"],
    )
    return ReglaEvaluada(regla, datos["valorCaso"], ResultadoRegla(datos["resultado"]))


def fila_decision(decision: Decision) -> dict[str, Any]:
    evaluacion = decision.evaluacion
    return {
        "id": decision.id,
        "codigo": decision.codigo,
        "cotizacion_id": decision.cotizacion_id,
        "oferta_id": decision.oferta_id,
        "cotizacion_codigo": decision.cotizacion_codigo,
        "cliente_id": decision.cliente_id,
        "producto_id": decision.producto_id,
        "idempotency_key": decision.idempotency_key,
        "huella_solicitud": decision.huella_solicitud,
        "resultado": evaluacion.resultado.value,
        "tipo": decision.tipo.value,
        "regla_determinante": evaluacion.regla_determinante,
        "reglas_evaluadas": Jsonb([_regla_evaluada(r) for r in evaluacion.reglas]),
        "motivos": Jsonb(list(evaluacion.motivos)),
        "insumos": Jsonb(insumos_canonicos(decision.insumos)),
        "hash_insumos": decision.hash_insumos,
        "version_reglas": decision.version_reglas,
        "hash_reglas": decision.hash_reglas,
        "decidida_en": decision.decidida_en,
        "duracion_ms": decision.duracion_ms,
        "correlation_id": decision.correlation_id,
        "actor_id": decision.actor_id,
    }


def decision_de(fila: Mapping[str, Any]) -> Decision:
    return Decision(
        id=fila["id"],
        codigo=fila["codigo"],
        cotizacion_id=fila["cotizacion_id"],
        oferta_id=fila["oferta_id"],
        cotizacion_codigo=fila["cotizacion_codigo"],
        cliente_id=fila["cliente_id"],
        producto_id=fila["producto_id"],
        tipo=TipoDecision(fila["tipo"]),
        evaluacion=Evaluacion(
            resultado=ResultadoDecision(fila["resultado"]),
            reglas=tuple(_de_regla_evaluada(r) for r in fila["reglas_evaluadas"]),
            regla_determinante=fila["regla_determinante"],
            motivos=tuple(fila["motivos"]),
        ),
        version_reglas=fila["version_reglas"],
        hash_reglas=fila["hash_reglas"],
        insumos=insumos_de(fila["insumos"]),
        hash_insumos=fila["hash_insumos"],
        decidida_en=fila["decidida_en"],
        duracion_ms=fila["duracion_ms"],
        idempotency_key=fila["idempotency_key"],
        huella_solicitud=fila["huella_solicitud"],
        correlation_id=fila["correlation_id"],
        actor_id=fila["actor_id"],
    )


def caso_de(fila: Mapping[str, Any]) -> CasoBandeja:
    return CasoBandeja(
        decision_id=fila["id"],
        codigo=fila["codigo"],
        cotizacion_id=fila["cotizacion_id"],
        cotizacion_codigo=fila["cotizacion_codigo"],
        cliente_id=fila["cliente_id"],
        producto_id=fila["producto_id"],
        moneda=fila["moneda"],
        suma_asegurada=Decimal(fila["suma_asegurada"]),
        motivos=tuple(fila["motivos"]),
        reglas_no_cumplidas=tuple(
            r["codigo"] for r in fila["reglas_evaluadas"] if r["resultado"] != "CUMPLE"
        ),
        decidida_en=fila["decidida_en"],
    )


def _escapar_like(texto: str) -> str:
    return texto.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def filtros_sql(filtros: FiltrosBandeja) -> tuple[list[str], list[Any]]:
    """Condiciones de la bandeja: casos en revisión, producto, motivo y búsqueda por código de
    decisión o por el inicio del identificador de la cotización (`COT-` + 8 caracteres)."""
    condiciones = [f"resultado = '{REVISION}'"]
    parametros: list[Any] = []
    if filtros.producto_id:
        condiciones.append("producto_id = %s")
        parametros.append(filtros.producto_id)
    if filtros.motivo:
        condiciones.append("motivos ? %s")
        parametros.append(filtros.motivo)
    if filtros.q:
        q = filtros.q.strip()
        cotizacion = q[4:] if q.upper().startswith("COT-") else q
        condiciones.append("(codigo ILIKE %s OR cotizacion_id::text ILIKE %s)")
        parametros.extend([_escapar_like(q) + "%", _escapar_like(cotizacion) + "%"])
    return condiciones, parametros


class DecisionesPg:
    """Implementa DecisionesRepositorio."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def guardar(self, decision: Decision) -> bool:
        fila = fila_decision(decision)
        async with self._pool.connection() as conexion, conexion.transaction():
            cursor = await conexion.execute(_INSERTAR, [fila[c] for c in COLUMNAS])
            if await cursor.fetchone() is None:
                return False
            await outbox_pg.insertar(conexion, [outbox_pg.evento_de(decision)])
        return True

    async def por_id(self, decision_id: UUID) -> Decision | None:
        return await self._una(_POR_ID, (decision_id,))

    async def por_cotizacion(self, cotizacion_id: UUID) -> Decision | None:
        return await self._una(_POR_COTIZACION, (cotizacion_id,))

    async def por_idempotencia(self, clave: UUID) -> Decision | None:
        return await self._una(_POR_IDEMPOTENCIA, (clave,))

    async def bandeja(
        self, filtros: FiltrosBandeja, limite: int, despues: tuple[datetime, UUID] | None
    ) -> list[CasoBandeja]:
        condiciones, parametros = filtros_sql(filtros)
        if despues is not None:
            condiciones.append("(decidida_en, id) > (%s, %s)")
            parametros.extend(despues)
        sql = (
            f"SELECT {_COLUMNAS_CASO} FROM decisiones_suscripcion "
            f"WHERE {' AND '.join(condiciones)} ORDER BY decidida_en, id LIMIT %s"
        )
        filas = await self._filas(sql, [*parametros, limite])
        return [caso_de(f) for f in filas]

    async def contar_bandeja(self, filtros: FiltrosBandeja) -> int:
        condiciones, parametros = filtros_sql(filtros)
        sql = f"SELECT count(*) AS total FROM decisiones_suscripcion WHERE {' AND '.join(condiciones)}"
        filas = await self._filas(sql, parametros)
        total: int = filas[0]["total"]
        return total

    async def contar_dia(self, desde: datetime, hasta: datetime) -> tuple[int, int, float | None]:
        async with self._pool.connection() as conexion:
            cursor = await conexion.execute(_CONTAR_DIA, (desde, hasta))
            fila = await cursor.fetchone()
        assert fila is not None  # un agregado siempre devuelve una fila
        resueltas, en_revision, media = fila
        return resueltas, en_revision, None if media is None else float(media)

    async def contar_fuera_del_compromiso(self, antes_de: datetime) -> int:
        async with self._pool.connection() as conexion:
            cursor = await conexion.execute(_CONTAR_FUERA, (antes_de,))
            fila = await cursor.fetchone()
        assert fila is not None
        total: int = fila[0]
        return total

    async def _una(self, sql: str, parametros: tuple[Any, ...]) -> Decision | None:
        filas = await self._filas(sql, parametros)
        return decision_de(filas[0]) if filas else None

    async def _filas(self, sql: str, parametros: Sequence[Any]) -> list[dict[str, Any]]:
        async with self._pool.connection() as conexion:
            cursor = conexion.cursor(row_factory=dict_row)
            await cursor.execute(sql, parametros)
            return await cursor.fetchall()
