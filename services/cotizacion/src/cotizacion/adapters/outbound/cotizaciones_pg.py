"""RepositorioCotizaciones en PostgreSQL (HU-W03). cotizacion es el único escritor de su base.

Los decimales del JSON van como texto (exactos); `prima_total` también va en su columna numérica
para consultas. La idempotencia es por actor: UNIQUE (actor_id, idempotency_key).
"""

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from cotizacion.domain.cotizacion.modelos import (
    Actor,
    Cobertura,
    Cotizacion,
    Desglose,
    EntradasPrima,
    EstadoCotizacion,
    FactorAplicado,
    RecargoAplicado,
    RolActor,
)
from cotizacion.domain.cotizacion.prima import desglose_canonico, entradas_canonicas
from cotizacion.domain.perfilamiento.errores import ConflictoIdempotencia
from cotizacion.domain.perfilamiento.modelos import NivelRiesgo

_RESTRICCION_IDEMPOTENCIA = "cotizaciones_actor_id_idempotency_key_key"

COLUMNAS = (
    "id",
    "cliente_id",
    "socio_id",
    "producto_id",
    "version_producto",
    "moneda",
    "periodicidad",
    "version_reglas",
    "hash_reglas",
    "perfil_id",
    "nivel_riesgo",
    "entradas",
    "coberturas",
    "desglose",
    "prima_total",
    "huella_calculo",
    "estado",
    "idempotency_key",
    "huella_solicitud",
    "calculado_en",
    "duracion_ms",
    "correlation_id",
    "actor_id",
    "actor_rol",
)


def _insertar(columnas: Sequence[str]) -> str:
    marcas = ", ".join(["%s"] * len(columnas))
    return f"INSERT INTO cotizaciones ({', '.join(columnas)}) VALUES ({marcas})"


_INSERTAR = _insertar(COLUMNAS)
_SELECT = f"SELECT {', '.join(COLUMNAS)} FROM cotizaciones"
_POR_ID = _SELECT + " WHERE id = %s"
_POR_IDEMPOTENCIA = _SELECT + " WHERE actor_id = %s AND idempotency_key = %s"


def fila_cotizacion(cotizacion: Cotizacion) -> dict[str, Any]:
    entradas = cotizacion.entradas
    return {
        "id": cotizacion.id,
        "cliente_id": cotizacion.cliente_id,
        "socio_id": cotizacion.socio_id,
        "producto_id": cotizacion.producto_id,
        "version_producto": cotizacion.version_producto,
        "moneda": cotizacion.moneda,
        "periodicidad": cotizacion.periodicidad,
        "version_reglas": cotizacion.version_reglas,
        "hash_reglas": cotizacion.hash_reglas,
        "perfil_id": cotizacion.perfil_id,
        "nivel_riesgo": entradas.nivel_riesgo.value if entradas.nivel_riesgo else None,
        "entradas": Jsonb(entradas_canonicas(entradas)),
        "coberturas": Jsonb(
            [
                {"codigo": c.codigo, "nombre": c.nombre, "obligatoria": c.obligatoria}
                for c in cotizacion.coberturas
            ]
        ),
        "desglose": Jsonb(desglose_canonico(cotizacion.desglose)),
        "prima_total": cotizacion.desglose.prima_total,
        "huella_calculo": cotizacion.huella_calculo,
        "estado": cotizacion.estado.value,
        "idempotency_key": cotizacion.idempotency_key,
        "huella_solicitud": cotizacion.huella_solicitud,
        "calculado_en": cotizacion.calculado_en,
        "duracion_ms": cotizacion.duracion_ms,
        "correlation_id": cotizacion.correlation_id,
        "actor_id": cotizacion.actor.id,
        "actor_rol": cotizacion.actor.rol.value,
    }


def _desglose(datos: Mapping[str, Any]) -> Desglose:
    return Desglose(
        prima_base=Decimal(datos["primaBase"]),
        factores=tuple(
            FactorAplicado(f["codigo"], f["valor"], Decimal(f["factor"])) for f in datos["factores"]
        ),
        prima_ajustada=Decimal(datos["primaAjustada"]),
        recargos=tuple(
            RecargoAplicado(r["cobertura"], Decimal(r["tasa"]), Decimal(r["valor"]))
            for r in datos["recargos"]
        ),
        subtotal=Decimal(datos["subtotal"]),
        ajuste_limite=Decimal(datos["ajusteLimite"]),
        prima_total=Decimal(datos["primaTotal"]),
    )


def cotizacion_de(fila: Mapping[str, Any]) -> Cotizacion:
    entradas = fila["entradas"]
    return Cotizacion(
        id=fila["id"],
        cliente_id=fila["cliente_id"],
        socio_id=fila["socio_id"],
        producto_id=fila["producto_id"],
        version_producto=fila["version_producto"],
        moneda=fila["moneda"],
        periodicidad=fila["periodicidad"],
        version_reglas=fila["version_reglas"],
        hash_reglas=fila["hash_reglas"],
        perfil_id=fila["perfil_id"],
        entradas=EntradasPrima(
            suma_asegurada=Decimal(entradas["sumaAsegurada"]),
            plazo_credito_meses=entradas["plazoCreditoMeses"],
            edad=entradas["edad"],
            nivel_riesgo=NivelRiesgo(entradas["nivelRiesgo"]) if entradas["nivelRiesgo"] else None,
            coberturas_opcionales=tuple(entradas["coberturasOpcionales"]),
        ),
        coberturas=tuple(
            Cobertura(c["codigo"], c["nombre"], c["obligatoria"]) for c in fila["coberturas"]
        ),
        desglose=_desglose(fila["desglose"]),
        huella_calculo=fila["huella_calculo"],
        estado=EstadoCotizacion(fila["estado"]),
        idempotency_key=fila["idempotency_key"],
        huella_solicitud=fila["huella_solicitud"],
        calculado_en=fila["calculado_en"],
        duracion_ms=fila["duracion_ms"],
        correlation_id=fila["correlation_id"],
        actor=Actor(fila["actor_id"], RolActor(fila["actor_rol"])),
    )


class CotizacionesPg:
    """Implementa RepositorioCotizaciones. Toma una conexión del pool solo durante cada operación."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def guardar(self, cotizacion: Cotizacion) -> None:
        fila = fila_cotizacion(cotizacion)
        try:
            async with self._pool.connection() as conexion:
                await conexion.execute(_INSERTAR, [fila[c] for c in COLUMNAS])
        except psycopg.errors.UniqueViolation as error:
            if error.diag.constraint_name == _RESTRICCION_IDEMPOTENCIA:
                raise ConflictoIdempotencia() from error
            raise

    async def por_idempotencia(self, actor_id: str, clave: UUID) -> Cotizacion | None:
        return await self._uno(_POR_IDEMPOTENCIA, (actor_id, clave))

    async def por_id(self, cotizacion_id: UUID) -> Cotizacion | None:
        return await self._uno(_POR_ID, (cotizacion_id,))

    async def _uno(self, sql: str, parametros: tuple[Any, ...]) -> Cotizacion | None:
        async with self._pool.connection() as conexion:
            cursor = conexion.cursor(row_factory=dict_row)
            await cursor.execute(sql, parametros)
            fila = await cursor.fetchone()
        return cotizacion_de(fila) if fila else None
