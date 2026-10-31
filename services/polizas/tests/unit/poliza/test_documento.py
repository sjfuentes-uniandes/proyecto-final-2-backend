"""Los datos del PDF de la póliza (HU-W17, 17.7; ADR 0014, decisión 4): el objeto inmutable que
reúne la póliza EMITIENDO, su aceptación, la instantánea de su decisión y el recaudo, sin nombres, y
la clave del documento en el bucket. La base real la prueba `tests/module/test_documento_w17.py`.
"""

import dataclasses
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Self
from uuid import UUID, uuid4

import pytest
from polizas.adapters.outbound.polizas_pg import PolizasPg
from polizas.application.poliza.documento import ReunirDatosDocumento
from polizas.domain.poliza.documento import (
    DatosDocumento,
    FuenteDocumento,
    clave_documento,
    datos_documento,
)
from polizas.domain.poliza.errores import DatosDocumentoNoDisponibles, PolizaInvalida
from polizas.domain.poliza.modelos import (
    Documento,
    OrigenPoliza,
    Poliza,
    Producto,
    Ramo,
    Vigencia,
)
from polizas.domain.suscripcion.modelos import Cobertura
from psycopg import sql
from solventa_common.inbox import TransaccionInboxPg

pytestmark = pytest.mark.anyio
BOG = timezone(timedelta(hours=-5))
CREADA = datetime(2026, 9, 13, 14, 0, tzinfo=UTC)
CLIENTE = UUID("00000000-0000-4000-8000-000000000001")
# W17-4: aceptada el 12/09/2026 en Bogotá.
ACEPTADA_EN = datetime(2026, 9, 12, 10, 0, tzinfo=BOG)
FECHA_DOCUMENTO = datetime(2026, 9, 13, 14, 5, tzinfo=UTC)
ORIGEN = OrigenPoliza(
    aceptacion_id=uuid4(),
    cotizacion_id=uuid4(),
    cliente_id=CLIENTE,
    producto=Producto("vida-hipotecario", "2026.09.1"),
    aceptada_en=ACEPTADA_EN,
)
POLIZA = Poliza.emitiendo(ORIGEN, secuencia=1, ahora=CREADA)
COBERTURAS = (
    Cobertura("MUERTE", "Muerte por cualquier causa", True),
    Cobertura("INCAPACIDAD_TOTAL_PERMANENTE", "Incapacidad total y permanente", True),
)
FUENTE = FuenteDocumento(
    coberturas=COBERTURAS,
    suma_asegurada=Decimal(180000000),
    prima=Decimal(64638),
    periodicidad="MENSUAL",
    moneda="COP",
    socio_id=None,
    aceptada_en=ACEPTADA_EN,
    transaccion_id="TX-SINTETICA-0001",
    version_reglas_prima="2026.09.1",
    version_reglas_suscripcion="2026.10.1",
)
# S01 en su forma canónica (contracts/datos-sinteticos/cotizaciones-suscripcion.json).
INSUMOS_S01: dict[str, Any] = {
    "cotizacionId": "00000000-0000-4000-a000-000000000001",
    "ofertaId": "00000000-0000-4000-e000-000000000001",
    "codigo": "COT-2026-00001",
    "clienteId": str(CLIENTE),
    "estadoIdentidad": "VERIFICADO",
    "socioId": "SOC-000123",
    "productoId": "vida-hipotecario",
    "versionProducto": "2026.09.1",
    "moneda": "COP",
    "periodicidad": "MENSUAL",
    "primaTotal": "64638",
    "versionReglasPrima": "2026.09.1",
    "huellaCalculo": "sha256:" + "a" * 64,
    "coberturas": [
        {"codigo": "MUERTE", "nombre": "Muerte por cualquier causa", "obligatoria": True},
        {
            "codigo": "INCAPACIDAD_TOTAL_PERMANENTE",
            "nombre": "Incapacidad total y permanente",
            "obligatoria": True,
        },
    ],
    "sumaAsegurada": "180000000",
    "plazoCreditoMeses": 180,
    "edad": 36,
    "calculadoEn": "2026-09-12T14:00:00Z",
    "vigenteHasta": "2026-09-16T14:00:00Z",
    "perfil": None,
    "coincidenciasListasRestrictivas": 0,
}


def _datos(**cambios: Any) -> DatosDocumento:
    return dataclasses.replace(datos_documento(POLIZA, FUENTE, FECHA_DOCUMENTO), **cambios)


# --- El objeto ---------------------------------------------------------------------------------
def test_reune_lo_que_lleva_el_pdf() -> None:
    datos = datos_documento(POLIZA, FUENTE, FECHA_DOCUMENTO)
    assert datos == DatosDocumento(
        numero=POLIZA.numero,
        ramo=Ramo("100", "Vida hipotecario"),
        producto=Producto("vida-hipotecario", "2026.09.1"),
        vigencia=Vigencia(date(2026, 9, 13), date(2027, 9, 12)),
        coberturas=COBERTURAS,
        suma_asegurada=Decimal(180000000),
        prima=Decimal(64638),
        periodicidad="MENSUAL",
        moneda="COP",
        cliente_id=CLIENTE,
        socio_id=None,
        aceptada_en=ACEPTADA_EN,
        transaccion_id="TX-SINTETICA-0001",
        version_reglas_prima="2026.09.1",
        version_reglas_suscripcion="2026.10.1",
        fecha_documento=FECHA_DOCUMENTO,
    )
    assert datos.numero.startswith("100-2026-")


def test_no_lleva_nombres_ni_datos_sensibles_de_los_insumos() -> None:
    # ADR 0014, decisión 4: el titular y el canal solo como identificadores.
    assert [c.name for c in dataclasses.fields(DatosDocumento)] == [
        "numero",
        "ramo",
        "producto",
        "vigencia",
        "coberturas",
        "suma_asegurada",
        "prima",
        "periodicidad",
        "moneda",
        "cliente_id",
        "socio_id",
        "aceptada_en",
        "transaccion_id",
        "version_reglas_prima",
        "version_reglas_suscripcion",
        "fecha_documento",
    ]


def test_es_inmutable() -> None:
    datos = _datos()
    with pytest.raises(dataclasses.FrozenInstanceError):
        datos.numero = "100-2026-000002"  # type: ignore[misc]


def test_con_socio_lo_lleva_como_identificador() -> None:
    fuente = dataclasses.replace(FUENTE, socio_id="SOC-000123")
    assert datos_documento(POLIZA, fuente, FECHA_DOCUMENTO).socio_id == "SOC-000123"


@pytest.mark.parametrize(
    ("cambios", "campo"),
    [
        ({"numero": "100-26-1"}, "numero"),
        ({"coberturas": ()}, "coberturas"),
        ({"suma_asegurada": 180000000.0}, "suma_asegurada"),
        ({"suma_asegurada": Decimal(0)}, "suma_asegurada"),
        ({"prima": Decimal(-1)}, "prima"),
        ({"prima": Decimal("NaN")}, "prima"),
        ({"prima": True}, "prima"),
        ({"moneda": "cop"}, "moneda"),
        ({"periodicidad": " "}, "periodicidad"),
        ({"cliente_id": str(CLIENTE)}, "cliente_id"),
        ({"socio_id": ""}, "socio_id"),
        ({"aceptada_en": ACEPTADA_EN.replace(tzinfo=None)}, "aceptada_en"),
        ({"transaccion_id": ""}, "transaccion_id"),
        ({"version_reglas_prima": ""}, "version_reglas_prima"),
        ({"version_reglas_suscripcion": None}, "version_reglas_suscripcion"),
        ({"fecha_documento": FECHA_DOCUMENTO.replace(tzinfo=None)}, "fecha_documento"),
    ],
    ids=lambda v: v if isinstance(v, str) else None,
)
def test_rechaza_datos_invalidos_nombrando_el_campo(cambios: dict[str, Any], campo: str) -> None:
    with pytest.raises(PolizaInvalida, match=f"^{campo}:"):
        _datos(**cambios)


# --- La clave en el bucket ---------------------------------------------------------------------
def test_la_clave_es_estable_por_poliza() -> None:
    clave = clave_documento(POLIZA)
    assert clave == f"polizas/{POLIZA.poliza_id}/{POLIZA.numero}.pdf"
    # No cambia con los puntos de control ni con un fallo: un reintento sube otra versión de ella.
    documento = Documento(clave, "v-1", "sha256:" + "c" * 64, 2048, FECHA_DOCUMENTO)
    otra = POLIZA.registrar_fallo("ALMACEN_NO_DISPONIBLE", CREADA).registrar_documento(
        documento, CREADA
    )
    assert clave_documento(otra) == clave
    assert clave_documento(Poliza.emitiendo(ORIGEN, secuencia=1, ahora=CREADA)) != clave


# --- El caso de uso, en la transacción recibida -----------------------------------------------
class FuentesFalsas:
    def __init__(self, fuente: FuenteDocumento | None) -> None:
        self.fuente = fuente
        self.llamadas: list[tuple[UUID, object]] = []

    async def fuente_documento(self, aceptacion_id: UUID, tx: object) -> FuenteDocumento | None:
        self.llamadas.append((aceptacion_id, tx))
        return self.fuente


async def test_reune_los_datos_en_la_transaccion_recibida() -> None:
    fuentes, tx = FuentesFalsas(FUENTE), object()
    datos = await ReunirDatosDocumento(fuentes).ejecutar(POLIZA, FECHA_DOCUMENTO, tx)
    assert datos == datos_documento(POLIZA, FUENTE, FECHA_DOCUMENTO)
    assert fuentes.llamadas == [(POLIZA.aceptacion_id, tx)]


async def test_sin_aceptacion_o_sin_recaudo_confirmado_no_hay_datos() -> None:
    with pytest.raises(DatosDocumentoNoDisponibles) as error:
        await ReunirDatosDocumento(FuentesFalsas(None)).ejecutar(POLIZA, FECHA_DOCUMENTO, object())
    assert error.value.codigo == "DATOS_DOCUMENTO_NO_DISPONIBLES"


# --- PolizasPg.fuente_documento, con una conexión falsa ---------------------------------------
class Cursor:
    def __init__(self, fila: Any) -> None:
        self.fila = fila
        self.sentencias: list[tuple[str, Any]] = []

    async def execute(self, consulta: str, parametros: Any = None) -> Self:
        self.sentencias.append((consulta, parametros))
        return self

    async def fetchone(self) -> Any:
        return self.fila


class Conexion:
    def __init__(self, fila: Any) -> None:
        self.cursor_ = Cursor(fila)

    def cursor(self, row_factory: Any = None) -> Cursor:
        return self.cursor_


def _tx(fila: Any) -> tuple[TransaccionInboxPg, Conexion]:
    conexion = Conexion(fila)
    return TransaccionInboxPg(conexion, sql.Identifier("inbox")), conexion  # type: ignore[arg-type]


async def test_la_fuente_sale_de_la_aceptacion_la_decision_y_el_recaudo_confirmado() -> None:
    tx, conexion = _tx(
        {
            "aceptada_en": ACEPTADA_EN,
            "insumos": INSUMOS_S01,
            "version_reglas": "2026.10.1",
            "transaccion_id": "TX-SINTETICA-0001",
        }
    )
    fuente = await PolizasPg().fuente_documento(POLIZA.aceptacion_id, tx)
    assert fuente == dataclasses.replace(FUENTE, socio_id="SOC-000123")
    [(consulta, parametros)] = conexion.cursor_.sentencias
    assert parametros == (POLIZA.aceptacion_id,)
    assert "JOIN decisiones_suscripcion d ON d.id = a.decision_id" in consulta
    assert "JOIN recaudos_aceptacion r ON r.clave_recaudo = a.id AND r.estado = 'CONFIRMADO'" in (
        consulta
    )


async def test_sin_fila_no_hay_fuente() -> None:
    tx, _ = _tx(None)
    assert await PolizasPg().fuente_documento(uuid4(), tx) is None
