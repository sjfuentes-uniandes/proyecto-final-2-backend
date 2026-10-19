"""Semilla de los clientes sintéticos C01–C19 (01 §3.5) para demostrar el perfilamiento en `int`.

Los datos de los proveedores del simulador solo existen para estos UUID; sin la semilla, un
cliente dado de alta por documento recibe un UUID aleatorio y ningún consentimiento. Solo corre
con `CLIENTES_SEMILLA_SINTETICA=true`, después de `migrar()`. Datos 100 % sintéticos.

Idempotente (`ON CONFLICT DO NOTHING`): nunca pisa lo que ya existe, así que lo que cambie en el
ambiente (p. ej. una revocación) se conserva entre arranques. No escribe en el outbox: no es una
operación de negocio, sino el estado inicial de un ambiente de pruebas.

Cada consentimiento reproduce la decisión de su caso en `decidir` (como el arnés de módulo de
`cotizacion`): vigente con los alcances permitidos, revocado, `ACTIVO` ya vencido sin
materializar (causa `VENCIDO`) o ninguno (`SIN_CONSENTIMIENTO`).
"""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from uuid import UUID

import psycopg

from clientes.domain.consentimientos.modelos import PROPOSITO_PERFILAMIENTO
from clientes.domain.identidad.modelos import EstadoIdentidad

logger = logging.getLogger("clientes.semilla")

TODOS_LOS_ALCANCES = (
    "FA_PRODUCTOS_VIGENTES",
    "FA_HISTORIAL_PAGOS_12M",
    "FA_INGRESOS_AGREGADOS",
    "DA_REGISTROS_PUBLICOS",
)
TIPO_DOCUMENTO = "CC"
FECHA_NACIMIENTO = date(1990, 4, 18)
PROVEEDOR = "SEMILLA_SINTETICA"
VERSION_TEXTO = "2026-10-17.1"
IDIOMA = "es-CO"
# Los instantes de los datos sintéticos (12 meses de vigencia, ADR 0005).
VERIFICADO_EN = datetime(2026, 10, 1, tzinfo=UTC)
OTORGADO_EN = datetime(2026, 10, 1, tzinfo=UTC)
VIGENTE_HASTA = datetime(2027, 10, 1, tzinfo=UTC)
REVOCADO_EN = datetime(2026, 10, 5, tzinfo=UTC)
VENCIDO_OTORGADO_EN = datetime(2025, 9, 1, tzinfo=UTC)
VENCIDO_VIGENTE_HASTA = datetime(2026, 9, 1, tzinfo=UTC)


class Situacion(StrEnum):
    """Lo que se siembra como consentimiento para `PERFILAMIENTO_PRECIO`."""

    VIGENTE = "VIGENTE"
    REVOCADO = "REVOCADO"
    VENCIDO = "VENCIDO"
    NINGUNO = "NINGUNO"


@dataclass(frozen=True, slots=True)
class ClienteSintetico:
    numero: int
    identidad: EstadoIdentidad = EstadoIdentidad.VERIFICADO
    situacion: Situacion = Situacion.VIGENTE
    alcances: tuple[str, ...] = TODOS_LOS_ALCANCES

    @property
    def codigo(self) -> str:
        return f"C{self.numero:02d}"

    @property
    def id(self) -> UUID:
        # NN hexadecimal: C10 = ...0a, C16 = ...10, C19 = ...13.
        return UUID(f"00000000-0000-4000-8000-{self.numero:012x}")

    @property
    def consentimiento_id(self) -> UUID:
        return UUID(f"00000000-0000-4000-9000-{self.numero:012x}")

    @property
    def numero_documento(self) -> str:
        # NN decimal: CC 9000000001 … CC 9000000019.
        return f"90000000{self.numero:02d}"

    @property
    def nombre_completo(self) -> str:
        return f"Cliente Prueba {self.numero:02d}"


def _sin(*excluidos: str) -> tuple[str, ...]:
    return tuple(a for a in TODOS_LOS_ALCANCES if a not in excluidos)


_EXCEPCIONES = {
    2: ClienteSintetico(2, alcances=_sin("FA_INGRESOS_AGREGADOS")),
    8: ClienteSintetico(8, situacion=Situacion.REVOCADO),
    9: ClienteSintetico(9, situacion=Situacion.VENCIDO),
    10: ClienteSintetico(10, situacion=Situacion.NINGUNO),
    11: ClienteSintetico(11, identidad=EstadoIdentidad.RECHAZADO),
    12: ClienteSintetico(12, identidad=EstadoIdentidad.PENDIENTE),
    16: ClienteSintetico(16, alcances=_sin("FA_HISTORIAL_PAGOS_12M")),
    17: ClienteSintetico(17, alcances=("DA_REGISTROS_PUBLICOS",)),
}
CLIENTES_SINTETICOS = tuple(_EXCEPCIONES.get(n, ClienteSintetico(n)) for n in range(1, 20))

# Sin destino en ON CONFLICT: ni un `id` ni un documento existentes se pisan.
_CLIENTE = (
    "INSERT INTO cliente (id, tipo_documento, numero_documento, nombre_completo, "
    "fecha_nacimiento, estado, verificado_en, proveedor, referencia) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING"
)
# Solo si el cliente es el sembrado (su documento pudo existir con otro UUID). El ON CONFLICT
# también cubre el índice de un solo ACTIVO por propósito.
_CONSENTIMIENTO = (
    "INSERT INTO consentimiento (id, cliente_id, proposito, alcances, version_texto, idioma, "
    "estado, otorgado_en, vigente_hasta, revocado_en) "
    "SELECT %s, cl.id, %s, %s, %s, %s, %s, %s, %s, %s FROM cliente cl WHERE cl.id = %s "
    "ON CONFLICT DO NOTHING"
)


def _identidad(cliente: ClienteSintetico) -> tuple[datetime | None, str | None, str | None]:
    """`verificado_en`, `proveedor` y `referencia`, como los deja `Cliente.con_resultado`."""
    if cliente.identidad is EstadoIdentidad.PENDIENTE:
        return None, None, None
    referencia = f"SEMILLA-{cliente.codigo}"
    if cliente.identidad is EstadoIdentidad.RECHAZADO:
        return None, PROVEEDOR, referencia
    return VERIFICADO_EN, PROVEEDOR, referencia


def _consentimiento(cliente: ClienteSintetico) -> tuple[object, ...] | None:
    """Parámetros de `_CONSENTIMIENTO`, o None si el caso no tiene consentimiento."""
    estado, otorgado, vigente, revocado = "ACTIVO", OTORGADO_EN, VIGENTE_HASTA, None
    if cliente.situacion is Situacion.NINGUNO:
        return None
    if cliente.situacion is Situacion.REVOCADO:
        estado, revocado = "REVOCADO", REVOCADO_EN
    elif cliente.situacion is Situacion.VENCIDO:
        otorgado, vigente = VENCIDO_OTORGADO_EN, VENCIDO_VIGENTE_HASTA
    return (
        cliente.consentimiento_id,
        PROPOSITO_PERFILAMIENTO,
        list(cliente.alcances),
        VERSION_TEXTO,
        IDIOMA,
        estado,
        otorgado,
        vigente,
        revocado,
        cliente.id,
    )


def sembrar(url: str) -> int:
    """Inserta los clientes sintéticos que falten, en una transacción. Devuelve cuántos creó."""
    creados = 0
    with psycopg.connect(url) as conexion, conexion.transaction():
        for cliente in CLIENTES_SINTETICOS:
            verificado_en, proveedor, referencia = _identidad(cliente)
            cursor = conexion.execute(
                _CLIENTE,
                (
                    cliente.id,
                    TIPO_DOCUMENTO,
                    cliente.numero_documento,
                    cliente.nombre_completo,
                    FECHA_NACIMIENTO,
                    cliente.identidad.value,
                    verificado_en,
                    proveedor,
                    referencia,
                ),
            )
            creados += cursor.rowcount
            parametros = _consentimiento(cliente)
            if parametros is not None:
                conexion.execute(_CONSENTIMIENTO, parametros)
    # Solo el conteo: los documentos y nombres, aunque sintéticos, tienen forma de PII.
    logger.info("semilla sintética aplicada", extra={"clientes_creados": creados})
    return creados
