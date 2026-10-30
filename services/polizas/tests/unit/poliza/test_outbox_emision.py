"""OutboxPolizasPg (HU-W17, 17.8): `PolizaEmitida` v1 en el outbox, dentro de la transacción
recibida (la de ACTIVA). La forma del evento la prueba `tests/contract/test_poliza_emitida_v1.py`."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from polizas.adapters.outbound.outbox_pg import OutboxPolizasPg
from polizas.adapters.outbound.polizas_pg import TransaccionPolizasPg
from polizas.domain.poliza.documento import FuenteDocumento, datos_documento
from polizas.domain.poliza.modelos import Documento, Firma, OrigenPoliza, Poliza, Producto
from polizas.domain.suscripcion.modelos import Cobertura

pytestmark = pytest.mark.anyio
CREADA = datetime(2026, 10, 22, 15, 0, 1, tzinfo=UTC)


class Conexion:
    def __init__(self) -> None:
        self.sentencias: list[tuple[str, Any]] = []

    async def execute(self, sql: str, parametros: Any = None) -> None:
        self.sentencias.append((sql, parametros))


def _activa() -> Poliza:
    origen = OrigenPoliza(
        aceptacion_id=uuid4(),
        cotizacion_id=uuid4(),
        cliente_id=UUID("00000000-0000-4000-8000-000000000001"),
        producto=Producto("vida-hipotecario", "2026.09.1"),
        aceptada_en=CREADA,
    )
    poliza = Poliza.emitiendo(origen, secuencia=3, ahora=CREADA, correlation_id="corr-1")
    documento = Documento(
        clave=f"polizas/{poliza.poliza_id}/{poliza.numero}.pdf",
        version_id="v-1",
        huella="sha256:" + "e" * 64,
        tamano_bytes=900,
        fecha_documento=CREADA,
    )
    return (
        poliza.registrar_documento(documento, CREADA)
        .registrar_firma(Firma("FIR-SINT-C01", CREADA), documento.huella, CREADA)
        .activar(CREADA)
    )


async def test_escribe_poliza_emitida_en_la_transaccion_recibida() -> None:
    activa = _activa()
    fuente = FuenteDocumento(
        coberturas=(Cobertura("MUERTE", "Muerte", True),),
        suma_asegurada=Decimal(180000000),
        prima=Decimal("64638.10"),
        periodicidad="MENSUAL",
        moneda="COP",
        socio_id=None,
        aceptada_en=CREADA,
        transaccion_id="TX-1",
        version_reglas_prima="2026.09.1",
        version_reglas_suscripcion="2026.10.1",
    )
    conexion = Conexion()
    tx = TransaccionPolizasPg(conexion)  # type: ignore[arg-type]
    await OutboxPolizasPg().emitida(activa, datos_documento(activa, fuente, CREADA), "corr-x", tx)

    [(sentencia, parametros)] = conexion.sentencias
    assert sentencia.startswith("INSERT INTO outbox (")
    event_id, aggregate_id, tipo, version, occurred_at, correlacion, payload = parametros
    assert isinstance(event_id, UUID)
    assert (aggregate_id, tipo, version) == (activa.poliza_id, "PolizaEmitida", 1)
    assert (occurred_at, correlacion) == (activa.emitida_en, "corr-x")
    assert payload.obj["documento"]["versionId"] == "v-1"
    assert payload.obj["prima"] == "64638.10"
