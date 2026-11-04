"""Semilla de pólizas de prueba para la proyección (`poliza_cliente`).

Solo corre con `CONSULTA_SEMILLA_POLIZAS=true` (int y local), después de `migrar()`. Lee
`semillas/polizas_semilla.json`. Idempotente (`ON CONFLICT DO NOTHING`): no pisa lo existente y
no escribe en el inbox, porque no es una operación de negocio.
"""

import json
import logging
from importlib import resources

import psycopg
from psycopg.types.json import Jsonb

logger = logging.getLogger("consulta.semilla")

_INSERTAR = (
    "INSERT INTO poliza_cliente (poliza_id, cliente_id, numero, estado, producto_id, "
    "producto_version, producto_nombre, ramo, vigencia_desde, vigencia_hasta, prima, "
    "suma_asegurada, moneda, periodicidad, coberturas, documento_huella, firma_id, "
    "emitida_en, proyectada_en) "
    "VALUES (%(polizaId)s, %(clienteId)s, %(numero)s, 'ACTIVA', %(productoId)s, "
    "%(productoVersion)s, %(productoNombre)s, %(ramo)s, %(vigenciaDesde)s, %(vigenciaHasta)s, "
    "%(prima)s, %(sumaAsegurada)s, %(moneda)s, %(periodicidad)s, %(coberturas)s, "
    "%(documentoHuella)s, %(firmaId)s, %(emitidaEn)s, now()) ON CONFLICT DO NOTHING"
)


def cargar() -> list[dict[str, object]]:
    texto = (
        resources.files("consulta.adapters.outbound")
        .joinpath("semillas/polizas_semilla.json")
        .read_text(encoding="utf-8")
    )
    return json.loads(texto)


def sembrar(url: str) -> int:
    """Inserta las pólizas que falten, en una transacción. Devuelve cuántas creó."""
    creadas = 0
    with psycopg.connect(url) as conexion, conexion.transaction():
        for poliza in cargar():
            parametros = {**poliza, "coberturas": Jsonb(poliza["coberturas"])}
            creadas += conexion.execute(_INSERTAR, parametros).rowcount
    logger.info("semilla de pólizas aplicada", extra={"polizas_creadas": creadas})
    return creadas
