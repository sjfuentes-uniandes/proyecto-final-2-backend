"""Modelo candidato 2026.10.1 convertido a ModeloRiesgo y datos de C01 (01 §3.5)."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from cotizacion.adapters.outbound.catalogo_http import ModeloRiesgoDto, a_modelo_riesgo
from cotizacion.domain.perfilamiento.modelo_riesgo import ModeloRiesgo
from cotizacion.domain.perfilamiento.modelos import (
    DatosDeclarados,
    DecisionConsentimiento,
    EstadoCampo,
    EstadoFuente,
    Fuente,
    Perfil,
    ResultadoFuente,
    ValorCampo,
    VerificacionConsentimiento,
)
from cotizacion.domain.perfilamiento.motor import FuenteEvaluada, evaluar

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


# --- Perfil de C01 calculado con el modelo candidato --------------------------------------

CAMPOS_FUENTE = {
    Fuente.FA_PRODUCTOS_VIGENTES: (
        "antiguedad_productos_anios",
        "cuota_mensual_obligaciones",
        "entidades_con_deuda",
    ),
    Fuente.FA_HISTORIAL_PAGOS_12M: ("moras_12m",),
    Fuente.FA_INGRESOS_AGREGADOS: ("ingreso_mensual_estimado",),
    Fuente.DA_REGISTROS_PUBLICOS: ("coincidencias_listas_restrictivas",),
}
CALCULADO_EN = datetime(2026, 10, 15, 15, 0, 0, 212000, tzinfo=UTC)


@pytest.fixture
def perfil_c01(modelo: ModeloRiesgo, fuentes_c01: dict[Fuente, FuenteEvaluada]) -> Perfil:
    fuentes = tuple(
        ResultadoFuente(
            fuente=fuente,
            estado=EstadoFuente.CONSULTADA,
            campos_solicitados=campos,
            campos={c: ValorCampo(EstadoCampo.DISPONIBLE, VARIABLES_C01[c]) for c in campos},
            version_fuente="1.8" if fuente is Fuente.DA_REGISTROS_PUBLICOS else "3.1",
            confianza=CONFIANZA_C01[fuente],
            consultada_en=datetime(2026, 10, 15, 15, 0, 0, 100000, tzinfo=UTC),
            duracion_ms=52,
            proveedor="datos-abiertos"
            if fuente is Fuente.DA_REGISTROS_PUBLICOS
            else "open-finance",
        )
        for fuente, campos in CAMPOS_FUENTE.items()
    )
    return Perfil(
        id=UUID("00000000-0000-4000-a000-000000000001"),
        cliente_id=UUID("00000000-0000-4000-8000-000000000001"),
        producto_id="vida-hipotecario",
        idempotency_key=UUID("00000000-0000-4000-b000-000000000001"),
        huella_solicitud="sha256:huella-c01",
        datos_declarados=DatosDeclarados(date(1990, 4, 18), 180, Decimal(180000000)),
        insumos=dict(VARIABLES_C01),
        resultado=evaluar(modelo, VARIABLES_C01, fuentes_c01),
        fuentes=fuentes,
        consentimiento=VerificacionConsentimiento(
            decision=DecisionConsentimiento.PERMITIDO,
            consentimiento_id=UUID("00000000-0000-4000-9000-000000000001"),
            vigente_hasta=datetime(2027, 10, 1, tzinfo=UTC),
            alcances_permitidos=frozenset(Fuente),
            causa=None,
        ),
        version_modelo=modelo.version,
        hash_modelo=modelo.hash,
        perfil_anterior_id=None,
        calculado_en=CALCULADO_EN,
        duracion_ms=212,
        correlation_id="corr-c01",
        actor_id="asesor-sintetico",
    )
