"""PolizasRepositorio en PostgreSQL (HU-W17, 17.6): tabla `polizas` de 004.

Todo corre en una transacción recibida (`tx.conexion`): la del inbox al crear la póliza con la
proyección del recaudo (16.13); la de la tarea emisora (17.8) para el resto. `crear` usa `ON
CONFLICT (aceptacion_id) DO NOTHING`: con dos entregas concurrentes, la segunda espera al commit de
la primera y no inserta. `actualizar` lleva la guarda `estado = 'EMITIENDO'`: si otra tarea ya la
activó, no cambia nada. El disparador de 004 protege la identidad y los puntos de control ya
registrados. Las migraciones las aplica `decisiones_pg.migrar` (todos los .sql en orden).

HU-W17 (17.7): `fuente_documento` lee, en la misma transacción, lo que el PDF toma de la aceptación,
de la instantánea de su decisión y del recaudo confirmado.

HU-W17 (17.8), la tarea emisora:
- `pendientes` (índice parcial `polizas_emitiendo` de 005), sin `FOR UPDATE`: no se bloquean filas
  durante las llamadas. Solo las que no tienen una reserva vigente: sin fallo (nunca intentadas, o
  de una tarea que cayó y cuya reserva venció), o con un fallo de hace más de `reintento_s`.
- `reservar` (17.8, ajuste) fija `reservada_hasta` si sigue EMITIENDO y no hay una reserva
  vigente, y devuelve la póliza al día; `liberar` borra solo la propia (por su hora). Al quedar
  ACTIVA, `actualizar` la libera en la misma escritura.
- La guarda de `actualizar` exige, además de EMITIENDO, que cada punto de control ya registrado
  sea el de la copia que se escribe. Si otra tarea registró otro documento u otra firma (o los
  registró y esta copia no los tiene), no cambia nada: la tarea relee, en lugar de chocar con el
  disparador de 004 o de pisar lo registrado con un fallo.
- `correlation_id` (005) es identidad: se escribe al crear y no está en `MUTABLES`.
- `TransaccionesPg` abre las transacciones cortas de la tarea sobre el pool.
"""

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from polizas.domain.poliza.documento import FuenteDocumento
from polizas.domain.poliza.modelos import (
    Documento,
    EstadoPoliza,
    Firma,
    OrigenPoliza,
    Poliza,
    Producto,
    Ramo,
    Vigencia,
)
from polizas.domain.suscripcion.insumos import insumos_de

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

# Lo que cambia una transición; la identidad (el resto) no cambia nunca.
MUTABLES = (
    "estado",
    "documento_clave",
    "documento_version_id",
    "documento_huella",
    "documento_tamano_bytes",
    "documento_fecha",
    "firma_id",
    "firma_sello_tiempo",
    "emitida_en",
    "ultimo_fallo",
    "actualizada_en",
)
COLUMNAS = (
    "poliza_id",
    "aceptacion_id",
    "cotizacion_id",
    "cliente_id",
    "producto_id",
    "producto_version",
    "ramo_codigo",
    "ramo_nombre",
    "numero",
    "vigencia_desde",
    "vigencia_hasta",
    "estado",
    "documento_clave",
    "documento_version_id",
    "documento_huella",
    "documento_tamano_bytes",
    "documento_fecha",
    "firma_id",
    "firma_sello_tiempo",
    "emitida_en",
    "ultimo_fallo",
    "creada_en",
    "actualizada_en",
    "correlation_id",
)
_INSERTAR = (
    f"INSERT INTO polizas ({', '.join(COLUMNAS)}) "
    f"VALUES ({', '.join(['%s'] * len(COLUMNAS))}) "
    "ON CONFLICT (aceptacion_id) DO NOTHING RETURNING poliza_id"
)
_POR_ACEPTACION = f"SELECT {', '.join(COLUMNAS)} FROM polizas WHERE aceptacion_id = %s"
_POR_ID = f"SELECT {', '.join(COLUMNAS)} FROM polizas WHERE poliza_id = %s"
# Un `= NULL` no es verdadero: una copia sin el punto de control solo pasa si sigue vacío. Al
# quedar ACTIVA se libera la reserva (después, el disparador de 004 no deja cambiar nada).
_ACTUALIZAR = (
    f"UPDATE polizas SET {', '.join(f'{c} = %s' for c in MUTABLES)}, "
    "reservada_hasta = CASE WHEN %s::text = 'ACTIVA' THEN NULL ELSE reservada_hasta END "
    "WHERE poliza_id = %s AND estado = 'EMITIENDO' "
    "AND (documento_version_id IS NULL OR documento_version_id = %s) "
    "AND (firma_id IS NULL OR (firma_id, firma_sello_tiempo) = (%s, %s)) "
    "RETURNING poliza_id"
)
# HU-W17 (17.8): sin reserva vigente; las sin fallo primero y de inmediato, las fallidas si el fallo
# es anterior a `reintento_antes_de`.
_SIN_RESERVA = "(reservada_hasta IS NULL OR reservada_hasta < %s)"
_PENDIENTES = (
    f"SELECT {', '.join(COLUMNAS)} FROM polizas "
    f"WHERE estado = 'EMITIENDO' AND {_SIN_RESERVA} "
    "AND (ultimo_fallo IS NULL OR actualizada_en < %s) "
    "ORDER BY ultimo_fallo IS NOT NULL, actualizada_en, poliza_id LIMIT %s"
)
_RESERVAR = (
    "UPDATE polizas SET reservada_hasta = %s "
    f"WHERE poliza_id = %s AND estado = 'EMITIENDO' AND {_SIN_RESERVA} "
    f"RETURNING {', '.join(COLUMNAS)}"
)
_LIBERAR = (
    "UPDATE polizas SET reservada_hasta = NULL "
    "WHERE poliza_id = %s AND estado = 'EMITIENDO' AND reservada_hasta = %s"
)
_SIGUIENTE = "SELECT nextval('polizas_numero')"
# El producto y su versión, de la instantánea de la decisión aceptada (DS2-10).
_ORIGEN = (
    "SELECT a.id AS aceptacion_id, a.cotizacion_id, a.cliente_id, a.aceptada_en, "
    "d.producto_id, d.insumos ->> 'versionProducto' AS producto_version "
    "FROM aceptaciones a JOIN decisiones_suscripcion d ON d.id = a.decision_id WHERE a.id = %s"
)
# HU-W17 (17.7): lo que el PDF toma de la aceptación, de la instantánea y del recaudo confirmado.
_FUENTE_DOCUMENTO = (
    "SELECT a.aceptada_en, d.insumos, d.version_reglas, r.transaccion_id "
    "FROM aceptaciones a "
    "JOIN decisiones_suscripcion d ON d.id = a.decision_id "
    "JOIN recaudos_aceptacion r ON r.clave_recaudo = a.id AND r.estado = 'CONFIRMADO' "
    "WHERE a.id = %s"
)


class TransaccionPg(Protocol):
    """Una transacción abierta: la del inbox (`TransaccionInboxPg`) o la de la tarea emisora."""

    @property
    def conexion(self) -> AsyncConnection[Any]: ...


def fila_poliza(poliza: Poliza) -> dict[str, Any]:
    documento, firma = poliza.documento, poliza.firma
    return {
        "poliza_id": poliza.poliza_id,
        "aceptacion_id": poliza.aceptacion_id,
        "cotizacion_id": poliza.cotizacion_id,
        "cliente_id": poliza.cliente_id,
        "producto_id": poliza.producto.id,
        "producto_version": poliza.producto.version,
        "ramo_codigo": poliza.ramo.codigo,
        "ramo_nombre": poliza.ramo.nombre,
        "numero": poliza.numero,
        "vigencia_desde": poliza.vigencia.desde,
        "vigencia_hasta": poliza.vigencia.hasta,
        "estado": poliza.estado.value,
        "documento_clave": documento and documento.clave,
        "documento_version_id": documento and documento.version_id,
        "documento_huella": documento and documento.huella,
        "documento_tamano_bytes": documento and documento.tamano_bytes,
        "documento_fecha": documento and documento.fecha_documento,
        "firma_id": firma and firma.firma_id,
        "firma_sello_tiempo": firma and firma.sello_tiempo,
        "emitida_en": poliza.emitida_en,
        "ultimo_fallo": poliza.ultimo_fallo,
        "creada_en": poliza.creada_en,
        "actualizada_en": poliza.actualizada_en,
        "correlation_id": poliza.correlation_id,
    }


def poliza_de(fila: Mapping[str, Any]) -> Poliza:
    # La migración garantiza que el documento y la firma vienen completos o no vienen.
    documento = (
        None
        if fila["documento_clave"] is None
        else Documento(
            clave=fila["documento_clave"],
            version_id=fila["documento_version_id"],
            huella=fila["documento_huella"],
            tamano_bytes=fila["documento_tamano_bytes"],
            fecha_documento=fila["documento_fecha"],
        )
    )
    firma = (
        None
        if fila["firma_id"] is None
        else Firma(firma_id=fila["firma_id"], sello_tiempo=fila["firma_sello_tiempo"])
    )
    return Poliza(
        poliza_id=fila["poliza_id"],
        aceptacion_id=fila["aceptacion_id"],
        cotizacion_id=fila["cotizacion_id"],
        cliente_id=fila["cliente_id"],
        producto=Producto(fila["producto_id"], fila["producto_version"]),
        ramo=Ramo(fila["ramo_codigo"], fila["ramo_nombre"]),
        numero=fila["numero"],
        vigencia=Vigencia(fila["vigencia_desde"], fila["vigencia_hasta"]),
        estado=EstadoPoliza(fila["estado"]),
        documento=documento,
        firma=firma,
        emitida_en=fila["emitida_en"],
        ultimo_fallo=fila["ultimo_fallo"],
        creada_en=fila["creada_en"],
        actualizada_en=fila["actualizada_en"],
        correlation_id=fila.get("correlation_id"),
    )


@dataclass(frozen=True, slots=True)
class TransaccionPolizasPg:
    """Una transacción de la tarea emisora (implementa TransaccionPg)."""

    conexion: AsyncConnection[Any]


class TransaccionesPg:
    """Implementa Transacciones[TransaccionPolizasPg] (HU-W17, 17.8): una conexión del pool por
    transacción corta; commit al salir y rollback con una excepción."""

    def __init__(self, pool: "AsyncConnectionPool[Any]") -> None:
        self._pool = pool

    @asynccontextmanager
    async def transaccion(self) -> AsyncIterator[TransaccionPolizasPg]:
        async with self._pool.connection() as conexion, conexion.transaction():
            yield TransaccionPolizasPg(conexion)


class PolizasPg:
    """Implementa PolizasRepositorio[TransaccionPg]."""

    async def origen(self, aceptacion_id: UUID, tx: TransaccionPg) -> OrigenPoliza | None:
        cursor = tx.conexion.cursor(row_factory=dict_row)
        await cursor.execute(_ORIGEN, (aceptacion_id,))
        fila = await cursor.fetchone()
        if fila is None:
            return None
        return OrigenPoliza(
            aceptacion_id=fila["aceptacion_id"],
            cotizacion_id=fila["cotizacion_id"],
            cliente_id=fila["cliente_id"],
            producto=Producto(fila["producto_id"], fila["producto_version"]),
            aceptada_en=fila["aceptada_en"],
        )

    async def siguiente_secuencia(self, tx: TransaccionPg) -> int:
        cursor = await tx.conexion.execute(_SIGUIENTE)
        fila = await cursor.fetchone()
        if fila is None:
            raise RuntimeError("poliza: la secuencia no devolvió valor")
        numero: int = fila[0]
        return numero

    async def crear(self, poliza: Poliza, tx: TransaccionPg) -> bool:
        fila = fila_poliza(poliza)
        cursor = await tx.conexion.execute(_INSERTAR, [fila[c] for c in COLUMNAS])
        return await cursor.fetchone() is not None

    async def por_aceptacion(self, aceptacion_id: UUID, tx: TransaccionPg) -> Poliza | None:
        cursor = tx.conexion.cursor(row_factory=dict_row)
        await cursor.execute(_POR_ACEPTACION, (aceptacion_id,))
        fila = await cursor.fetchone()
        return None if fila is None else poliza_de(fila)

    async def actualizar(self, poliza: Poliza, tx: TransaccionPg) -> bool:
        fila = fila_poliza(poliza)
        guarda = [
            poliza.poliza_id,
            fila["documento_version_id"],
            fila["firma_id"],
            fila["firma_sello_tiempo"],
        ]
        cursor = await tx.conexion.execute(
            _ACTUALIZAR, [*(fila[c] for c in MUTABLES), fila["estado"], *guarda]
        )
        return await cursor.fetchone() is not None

    async def por_id(self, poliza_id: UUID, tx: TransaccionPg) -> Poliza | None:
        cursor = tx.conexion.cursor(row_factory=dict_row)
        await cursor.execute(_POR_ID, (poliza_id,))
        fila = await cursor.fetchone()
        return None if fila is None else poliza_de(fila)

    async def pendientes(
        self, ahora: datetime, reintento_antes_de: datetime, lote: int, tx: TransaccionPg
    ) -> list[Poliza]:
        """Implementa EmisionesRepositorio[TransaccionPg] (HU-W17, 17.8)."""
        cursor = tx.conexion.cursor(row_factory=dict_row)
        await cursor.execute(_PENDIENTES, (ahora, reintento_antes_de, lote))
        return [poliza_de(fila) for fila in await cursor.fetchall()]

    async def reservar(
        self, poliza_id: UUID, ahora: datetime, hasta: datetime, tx: TransaccionPg
    ) -> Poliza | None:
        cursor = tx.conexion.cursor(row_factory=dict_row)
        await cursor.execute(_RESERVAR, (hasta, poliza_id, ahora))
        fila = await cursor.fetchone()
        return None if fila is None else poliza_de(fila)

    async def liberar(self, poliza_id: UUID, hasta: datetime, tx: TransaccionPg) -> None:
        await tx.conexion.execute(_LIBERAR, (poliza_id, hasta))

    async def fuente_documento(
        self, aceptacion_id: UUID, tx: TransaccionPg
    ) -> FuenteDocumento | None:
        """Implementa FuentesDocumento[TransaccionPg] (HU-W17, 17.7)."""
        cursor = tx.conexion.cursor(row_factory=dict_row)
        await cursor.execute(_FUENTE_DOCUMENTO, (aceptacion_id,))
        fila = await cursor.fetchone()
        if fila is None:
            return None
        # Los insumos en su forma canónica; solo se toma lo que va en el PDF.
        insumos = insumos_de(fila["insumos"])
        return FuenteDocumento(
            coberturas=insumos.coberturas,
            suma_asegurada=insumos.suma_asegurada,
            prima=insumos.prima_total,
            periodicidad=insumos.periodicidad,
            moneda=insumos.moneda,
            socio_id=insumos.socio_id,
            aceptada_en=fila["aceptada_en"],
            transaccion_id=fila["transaccion_id"],
            version_reglas_prima=insumos.version_reglas_prima,
            version_reglas_suscripcion=fila["version_reglas"],
        )
