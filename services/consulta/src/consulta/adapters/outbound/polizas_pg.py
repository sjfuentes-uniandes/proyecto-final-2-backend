"""ProyeccionPolizas en PostgreSQL (HU-M10): tabla `poliza_cliente` de 001.

`insertar` escribe en la transacción del inbox (`TransaccionInboxPg.conexion`). `ON CONFLICT
(poliza_id) DO NOTHING`: la póliza ACTIVA es inmutable, así que un duplicado no modifica nada. Las
migraciones las aplica `migrar` (todos los .sql en orden).
"""

from importlib import resources

import psycopg
from psycopg.types.json import Jsonb
from solventa_common.inbox import TransaccionInboxPg

from consulta.domain.polizas.modelos import PolizaProyectada

_PAQUETE = resources.files("consulta.adapters.outbound")
_LOCK = "SELECT pg_advisory_lock(hashtext('consulta.migraciones'))"
_UNLOCK = "SELECT pg_advisory_unlock(hashtext('consulta.migraciones'))"

COLUMNAS = (
    "poliza_id",
    "cliente_id",
    "numero",
    "estado",
    "producto_id",
    "producto_version",
    "producto_nombre",
    "ramo",
    "vigencia_desde",
    "vigencia_hasta",
    "prima",
    "suma_asegurada",
    "moneda",
    "periodicidad",
    "coberturas",
    "documento_huella",
    "firma_id",
    "emitida_en",
    "proyectada_en",
)
_INSERTAR = (
    f"INSERT INTO poliza_cliente ({', '.join(COLUMNAS)}) "
    f"VALUES ({', '.join(['%s'] * len(COLUMNAS))}) "
    "ON CONFLICT (poliza_id) DO NOTHING RETURNING poliza_id"
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


def fila_de(poliza: PolizaProyectada) -> list[object]:
    """Montos como Decimal: psycopg los envía como numeric, sin pasar por float."""
    return [
        poliza.poliza_id,
        poliza.cliente_id,
        poliza.numero,
        poliza.estado,
        poliza.producto_id,
        poliza.producto_version,
        poliza.producto_nombre,
        poliza.ramo,
        poliza.vigencia.desde,
        poliza.vigencia.hasta,
        poliza.prima,
        poliza.suma_asegurada,
        poliza.moneda,
        poliza.periodicidad,
        Jsonb(
            [
                {"codigo": c.codigo, "nombre": c.nombre, "obligatoria": c.obligatoria}
                for c in poliza.coberturas
            ]
        ),
        poliza.documento_huella,
        poliza.firma_id,
        poliza.emitida_en,
        poliza.proyectada_en,
    ]


class ProyeccionPolizasPg:
    """Implementa ProyeccionPolizas[TransaccionInboxPg]."""

    async def insertar(self, poliza: PolizaProyectada, tx: TransaccionInboxPg) -> bool:
        cursor = await tx.conexion.execute(_INSERTAR, fila_de(poliza))
        return await cursor.fetchone() is not None
