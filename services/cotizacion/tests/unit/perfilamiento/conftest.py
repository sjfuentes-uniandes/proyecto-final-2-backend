"""Modelo candidato 2026.10.1 convertido a ModeloRiesgo y datos de C01 (01 §3.5)."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from cotizacion.adapters.outbound.catalogo_http import ModeloRiesgoDto, a_modelo_riesgo
from cotizacion.domain.perfilamiento.modelo_riesgo import ModeloRiesgo
from cotizacion.domain.perfilamiento.modelos import EstadoFuente, Fuente
from cotizacion.domain.perfilamiento.motor import FuenteEvaluada

# La semilla que publica catalogo: las pruebas evalúan exactamente el modelo desplegado.
SEMILLA = (
    Path(__file__).resolve().parents[4]
    / "catalogo/src/catalogo/adapters/outbound/semillas/modelo_2026_10_1.json"
)

# C01: declarados por defecto (edad a 2026-10-15) y respuestas nominales del simulador.
VARIABLES_C01 = {
    "edad": Decimal(36),
    "plazo_credito_meses": Decimal(180),
    "suma_asegurada": Decimal(180000000),
    "antiguedad_productos_anios": Decimal(7),
    "cuota_mensual_obligaciones": Decimal("1800000.00"),
    "entidades_con_deuda": Decimal(2),
    "moras_12m": Decimal(0),
    "ingreso_mensual_estimado": Decimal("6500000.00"),
    "coincidencias_listas_restrictivas": Decimal(0),
}
CONFIANZA_C01 = {
    Fuente.FA_PRODUCTOS_VIGENTES: Decimal("0.95"),
    Fuente.FA_HISTORIAL_PAGOS_12M: Decimal("0.91"),
    Fuente.FA_INGRESOS_AGREGADOS: Decimal("0.93"),
    Fuente.DA_REGISTROS_PUBLICOS: Decimal("0.88"),
}


@pytest.fixture
def definicion_candidata() -> dict[str, Any]:
    definicion: dict[str, Any] = json.loads(SEMILLA.read_text(encoding="utf-8"))
    return definicion


def respuesta_catalogo(definicion: dict[str, Any], version: str = "2026.10.1") -> dict[str, Any]:
    return {
        "version": version,
        "productoId": "vida-hipotecario",
        "vigenteDesde": "2026-10-01T00:00:00Z",
        "hash": f"sha256:{version}",
        "definicion": definicion,
    }


@pytest.fixture
def respuesta_candidata(definicion_candidata: dict[str, Any]) -> dict[str, Any]:
    return respuesta_catalogo(definicion_candidata)


@pytest.fixture
def modelo(respuesta_candidata: dict[str, Any]) -> ModeloRiesgo:
    return a_modelo_riesgo(ModeloRiesgoDto.model_validate(respuesta_candidata))


@pytest.fixture
def variables_c01() -> dict[str, Decimal]:
    return dict(VARIABLES_C01)


@pytest.fixture
def fuentes_c01() -> dict[Fuente, FuenteEvaluada]:
    return {f: FuenteEvaluada(EstadoFuente.CONSULTADA, c) for f, c in CONFIANZA_C01.items()}
