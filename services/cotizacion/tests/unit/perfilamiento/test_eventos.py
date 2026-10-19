"""Eventos del outbox: sobre completo y payloads sin valores financieros (W11-P09)."""

import json
from dataclasses import replace
from uuid import UUID

from cotizacion.adapters.outbound.outbox_pg import eventos_de
from cotizacion.domain.perfilamiento.modelos import (
    DecisionConsentimiento,
    EstadoCampo,
    Perfil,
    ValorCampo,
)

# 01 §3.9: valores de C01 que nunca salen del dominio.
CENTINELAS = ("1800000", "6500000", "180000000", "1990-04-18", "0.95", "75.9", "0.92")


def _json(perfil: Perfil) -> list[dict]:
    return [s.model_dump(mode="json", by_alias=True) for s in eventos_de(perfil, "corr-evento")]


def test_dos_eventos_con_sobre_completo(perfil_c01: Perfil) -> None:
    calculado, consultados = _json(perfil_c01)
    for sobre in (calculado, consultados):
        assert UUID(sobre["eventId"])
        assert sobre["aggregateId"] == str(perfil_c01.id)
        assert sobre["occurredAt"] == "2026-10-15T15:00:00.212000Z"
        assert (sobre["version"], sobre["correlationId"]) == (1, "corr-evento")
    assert calculado["eventId"] != consultados["eventId"]
    assert (calculado["type"], consultados["type"]) == (
        "PerfilRiesgoCalculado",
        "DatosConsentidosConsultados",
    )


def test_perfil_riesgo_calculado(perfil_c01: Perfil) -> None:
    payload = _json(perfil_c01)[0]["payload"]
    assert payload["estado"] == "CALCULADO" and payload["nivelRiesgo"] == "BAJO"
    assert (payload["versionModelo"], payload["actorId"]) == ("2026.10.1", "asesor-sintetico")
    assert payload["factores"][0] == {
        "codigo": "PUNTUALIDAD_PAGOS",
        "direccion": "REDUCE_RIESGO",
        "estado": "EVALUADO",
    }
    assert payload["datosRequeridos"] == [] and payload["perfilAnteriorId"] is None


def test_datos_consentidos_consultados(perfil_c01: Perfil) -> None:
    payload = _json(perfil_c01)[1]["payload"]
    assert payload["consentimientoId"] == "00000000-0000-4000-9000-000000000001"
    assert payload["proposito"] == "PERFILAMIENTO_PRECIO"
    assert [f["fuente"] for f in payload["fuentes"]] == [
        "FA_PRODUCTOS_VIGENTES",
        "FA_HISTORIAL_PAGOS_12M",
        "FA_INGRESOS_AGREGADOS",
        "DA_REGISTROS_PUBLICOS",
    ]
    assert payload["fuentes"][0]["camposDisponibles"] == [
        "antiguedad_productos_anios",
        "cuota_mensual_obligaciones",
        "entidades_con_deuda",
    ]
    assert set(payload["fuentes"][0]) == {
        "fuente",
        "estado",
        "camposSolicitados",
        "camposDisponibles",
        "versionFuente",
        "consultadaEn",
    }


def test_campo_no_disponible_no_figura_como_disponible(perfil_c01: Perfil) -> None:
    historial = perfil_c01.fuentes[1]
    sin_moras = replace(
        historial, campos={"moras_12m": ValorCampo(EstadoCampo.NO_DISPONIBLE, None)}
    )
    perfil = replace(
        perfil_c01, fuentes=(perfil_c01.fuentes[0], sin_moras, *perfil_c01.fuentes[2:])
    )
    fuente = _json(perfil)[1]["payload"]["fuentes"][1]
    assert (fuente["camposSolicitados"], fuente["camposDisponibles"]) == (["moras_12m"], [])


def test_sin_valores_financieros_ni_declarados(perfil_c01: Perfil) -> None:
    texto = json.dumps(_json(perfil_c01))
    for centinela in CENTINELAS:
        assert centinela not in texto


def test_sin_consentimiento_el_id_es_nulo(perfil_c01: Perfil) -> None:
    rechazado = replace(
        perfil_c01.consentimiento,
        decision=DecisionConsentimiento.RECHAZADO,
        consentimiento_id=None,
    )
    for consentimiento in (rechazado, None):
        perfil = replace(perfil_c01, consentimiento=consentimiento)
        assert _json(perfil)[1]["payload"]["consentimientoId"] is None


def test_sin_comprobacion_final_el_evento_no_lleva_marca(perfil_c01: Perfil) -> None:
    payload = _json(perfil_c01)[1]["payload"]
    assert (payload["comprobacionFinal"], payload["revocadoDurantePerfilamiento"]) == (None, False)


def test_la_comprobacion_final_viaja_en_datos_consentidos_consultados(
    perfil_c01: Perfil,
) -> None:
    rechazo = replace(
        perfil_c01.consentimiento,
        decision=DecisionConsentimiento.RECHAZADO,
        consentimiento_id=None,
        vigente_hasta=None,
        alcances_permitidos=frozenset(),
        causa="VENCIDO",
    )
    calculado, consultados = _json(replace(perfil_c01, comprobacion_final=rechazo))
    assert consultados["payload"]["comprobacionFinal"] == {
        "decision": "RECHAZADO",
        "causa": "VENCIDO",
    }
    assert consultados["payload"]["revocadoDurantePerfilamiento"] is True
    # El id del consentimiento de la fase A se conserva; PerfilRiesgoCalculado no cambia.
    assert consultados["payload"]["consentimientoId"] == "00000000-0000-4000-9000-000000000001"
    assert "comprobacionFinal" not in calculado["payload"]
