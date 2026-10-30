"""PolizasRepositorio en PostgreSQL (HU-W17, 17.6): tabla `polizas` de 004.

Todo corre en una transacción recibida (`tx.conexion`): la del inbox al crear la póliza con la
proyección del recaudo (16.13); la de la tarea emisora (17.8) para el resto. `crear` usa `ON
CONFLICT (aceptacion_id) DO NOTHING`: con dos entregas concurrentes, la segunda espera al commit de
la primera y no inserta. `actualizar` lleva la guarda `estado = 'EMITIENDO'`: si otra tarea ya la
activó, no cambia nada. El disparador de 004 protege la identidad y los puntos de control ya
registrados. Las migraciones las aplica `decisiones_pg.migrar` (todos los .sql en orden).
"""

from collections.abc import Mapping
from typing import Any, Protocol
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

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
)
_INSERTAR = (
    f"INSERT INTO polizas ({', '.join(COLUMNAS)}) "
    f"VALUES ({', '.join(['%s'] * len(COLUMNAS))}) "
    "ON CONFLICT (aceptacion_id) DO NOTHING RETURNING poliza_id"
)
_POR_ACEPTACION = f"SELECT {', '.join(COLUMNAS)} FROM polizas WHERE aceptacion_id = %s"
_ACTUALIZAR = (
    f"UPDATE polizas SET {', '.join(f'{c} = %s' for c in MUTABLES)} "
    "WHERE poliza_id = %s AND estado = 'EMITIENDO' RETURNING poliza_id"
)
_SIGUIENTE = "SELECT nextval('polizas_numero')"
# El producto y su versión, de la instantánea de la decisión aceptada (DS2-10).
_ORIGEN = (
    "SELECT a.id AS aceptacion_id, a.cotizacion_id, a.cliente_id, a.aceptada_en, "
    "d.producto_id, d.insumos ->> 'versionProducto' AS producto_version "
    "FROM aceptaciones a JOIN decisiones_suscripcion d ON d.id = a.decision_id WHERE a.id = %s"
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
    )


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
        cursor = await tx.conexion.execute(
            _ACTUALIZAR, [*(fila[c] for c in MUTABLES), poliza.poliza_id]
        )
        return await cursor.fetchone() is not None
