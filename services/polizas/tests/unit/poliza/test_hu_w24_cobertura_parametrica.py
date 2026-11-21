"""HU-W24: la cobertura paramétrica de la cotización llega a `PolizaEmitida.coberturaParametrica`.

`cotizacion` la entrega en los insumos de la oferta; `polizas` la guarda con la decisión (forma
canónica), la lleva en los datos de la emisión (no en el PDF) y la publica en el evento. Sin ella, la
huella de los insumos y el evento no cambian.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from jsonschema import Draft202012Validator
from polizas.adapters.outbound.catalogo_http import a_reglas
from polizas.adapters.outbound.cotizacion_http import a_insumos
from polizas.adapters.outbound.eventos import POLIZA_EMITIDA
from polizas.adapters.outbound.outbox_pg import OutboxPolizasPg, evento_de_emision
from polizas.adapters.outbound.polizas_pg import TransaccionPolizasPg
from polizas.domain.poliza.documento import FuenteDocumento, datos_documento
from polizas.domain.poliza.modelos import Documento, Firma, OrigenPoliza, Poliza, Producto
from polizas.domain.suscripcion.insumos import hash_insumos, insumos_canonicos, insumos_de
from polizas.domain.suscripcion.modelos import Cobertura, CoberturaParametrica, ResultadoDecision
from polizas.domain.suscripcion.motor import evaluar

pytestmark = pytest.mark.anyio
RAIZ = Path(__file__).parents[5]
ESQUEMA = json.loads(
    (RAIZ / "contracts/events" / f"{POLIZA_EMITIDA}.v1.schema.json").read_text(encoding="utf-8")
)
# Los insumos de un caso de los datos sintéticos de suscripción (como entrega cotizacion).
INSUMOS = json.loads(
    (RAIZ / "contracts/datos-sinteticos/cotizaciones-suscripcion.json").read_text(encoding="utf-8")
)["casos"][0]["insumos"]
CREADA = datetime(2026, 10, 22, 15, 0, 1, tzinfo=UTC)
CANONICA = {
    "tipoEvento": "LLUVIA_ACUMULADA",
    "zona": "11001",
    "umbral": "120",
    "unidad": "mm",
    "valorPago": "1400000.00",
    "moneda": "COP",
    "versionRegla": "2026.11.1",
}
COBERTURA = CoberturaParametrica(
    tipo_evento="LLUVIA_ACUMULADA",
    zona="11001",
    umbral="120",
    unidad="mm",
    valor_pago="1400000.00",
    moneda="COP",
    version_regla="2026.11.1",
)


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
        producto=Producto("clima-lluvia", "2026.11.1"),
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
        .registrar_firma(Firma("FIR-SINT-W24", CREADA), documento.huella, CREADA)
        .activar(CREADA)
    )


# --- Insumos ---------------------------------------------------------------------------------
def test_sin_cobertura_la_forma_canonica_y_la_huella_no_cambian() -> None:
    base = insumos_de(INSUMOS)
    assert base.cobertura_parametrica is None
    assert "coberturaParametrica" not in insumos_canonicos(base)
    assert hash_insumos(base) == hash_insumos(insumos_de(json.loads(json.dumps(INSUMOS))))


def test_con_cobertura_la_forma_canonica_la_conserva_y_vuelve_igual() -> None:
    con = replace(insumos_de(INSUMOS), cobertura_parametrica=COBERTURA)
    canonicos = insumos_canonicos(con)
    assert canonicos["coberturaParametrica"] == CANONICA
    assert insumos_de(canonicos) == con
    assert hash_insumos(con) != hash_insumos(insumos_de(INSUMOS))


@pytest.mark.parametrize("valor", ["ausente", None, CANONICA])
def test_los_insumos_de_cotizacion_con_y_sin_cobertura(valor: Any) -> None:
    datos = dict(INSUMOS)
    if valor != "ausente":
        datos["coberturaParametrica"] = valor
    leidos = a_insumos(json.dumps(datos).encode())
    assert leidos.cobertura_parametrica == (COBERTURA if valor == CANONICA else None)
    # Sin cobertura, la forma guardada es la de antes de HU-W24.
    assert ("coberturaParametrica" in insumos_canonicos(leidos)) is (valor == CANONICA)


# --- Emisión ---------------------------------------------------------------------------------
def _fuente(cobertura: CoberturaParametrica | None) -> FuenteDocumento:
    return FuenteDocumento(
        coberturas=(Cobertura("LLUVIA_ACUMULADA", "Lluvia acumulada", True),),
        suma_asegurada=Decimal(1400000),
        prima=Decimal("35000.00"),
        periodicidad="MENSUAL",
        moneda="COP",
        socio_id=None,
        aceptada_en=CREADA,
        transaccion_id="TX-1",
        version_reglas_prima="2026.11.1",
        version_reglas_suscripcion="2026.10.1",
        cobertura_parametrica=cobertura,
    )


def test_los_datos_de_la_emision_llevan_la_cobertura_de_la_fuente() -> None:
    activa = _activa()
    assert datos_documento(activa, _fuente(COBERTURA), CREADA).cobertura_parametrica == COBERTURA
    assert datos_documento(activa, _fuente(None), CREADA).cobertura_parametrica is None


async def test_poliza_emitida_publica_la_cobertura_parametrica_y_cumple_el_esquema() -> None:
    activa = _activa()
    conexion = Conexion()
    tx = TransaccionPolizasPg(conexion)  # type: ignore[arg-type]
    datos = datos_documento(activa, _fuente(COBERTURA), CREADA)

    await OutboxPolizasPg().emitida(activa, datos, "corr-w24", tx)

    [(_, parametros)] = conexion.sentencias
    payload = parametros[-1].obj
    assert payload["coberturaParametrica"] == CANONICA
    sobre = evento_de_emision(activa, datos, "corr-w24").model_dump(mode="json", by_alias=True)
    validador = Draft202012Validator(ESQUEMA, format_checker=Draft202012Validator.FORMAT_CHECKER)
    assert [e.message for e in validador.iter_errors(sobre)] == []
    assert sobre["payload"]["coberturaParametrica"] == CANONICA


def test_una_regla_fuera_del_contrato_no_se_publica() -> None:
    activa = _activa()
    invalida = replace(COBERTURA, tipo_evento="GRANIZO")
    with pytest.raises(ValueError):
        evento_de_emision(activa, datos_documento(activa, _fuente(invalida), CREADA), "c")


# --- Suscripción del paramétrico (sin perfil) ------------------------------------------------
REGLAS_CLIMA = (
    RAIZ
    / "services/catalogo/src/catalogo/adapters/outbound/semillas/qa"
    / "reglas_suscripcion_qa_clima_lluvia_2026_11_2.json"
)


def test_clima_lluvia_sin_perfil_se_aprueba_sin_pasar_por_revision() -> None:
    """Las reglas vigentes de `clima-lluvia` (semilla QA del catálogo) solo usan datos de la
    cotización: un cliente cotizado sin perfil queda APROBADO, no en la bandeja del suscriptor."""
    definicion = json.loads(REGLAS_CLIMA.read_text(encoding="utf-8"))
    respuesta = {
        "productoId": "clima-lluvia",
        "version": "2026.11.2",
        "vigenteDesde": "2026-11-21T00:00:00Z",
        "hash": "sha256:" + "0" * 64,
        "definicion": definicion,
    }
    reglas = a_reglas(json.dumps(respuesta).encode())
    sin_perfil = replace(insumos_de(INSUMOS), perfil=None, coincidencias_listas_restrictivas=None)
    evaluacion = evaluar(sin_perfil, reglas.reglas)
    assert evaluacion.resultado is ResultadoDecision.APROBADA, evaluacion
