"""Cierre backend de HU-W10 (W10-P12, MT-W10-01 a 07): solo se consultan los datos
autorizados y su procedencia queda asociada al perfil. Usa el arnés de W11-P14."""

import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

PRODUCTOS = "FA_PRODUCTOS_VIGENTES"
HISTORIAL = "FA_HISTORIAL_PAGOS_12M"
INGRESOS = "FA_INGRESOS_AGREGADOS"
REGISTROS = "DA_REGISTROS_PUBLICOS"
CAMPOS_PEDIDOS = {
    "active-products": ("fields", "creditorCount,oldestProductYears,totalMonthlyInstallment"),
    "payment-history": ("fields", "lateCount"),
    "aggregated-income": ("fields", "estimatedMonthlyIncome"),
}


def cliente_id(nn: str) -> str:
    return f"00000000-0000-4000-8000-0000000000{nn}"


def _recurso(solicitud: dict[str, Any]) -> str:
    return urlsplit(solicitud["url"]).path.rsplit("/", 1)[-1]


def _fuentes(perfil: dict[str, Any]) -> dict[str, str]:
    return {f["fuente"]: f["estado"] for f in perfil["fuentes"]}


def test_mt_w10_01_c01_campos_exactos_y_linaje(cotizacion: Any) -> None:
    svc = cotizacion()
    perfil = svc.enviar("01").json()
    solicitudes = svc.wiremock.a_aliados(cliente_id("01"))
    assert len(solicitudes) == 4
    for solicitud in solicitudes:
        consulta = parse_qs(urlsplit(solicitud["url"]).query)
        if solicitud["url"].startswith("/datos-abiertos"):
            assert consulta["campos"] == ["listasRestrictivas"]
        else:
            parametro, valor = CAMPOS_PEDIDOS[_recurso(solicitud)]
            assert consulta[parametro] == [valor]
    linaje = svc.bases.filas(
        "SELECT fuente, campos_solicitados, campos_disponibles, version_fuente, consultada_en, "
        "proveedor FROM linaje_fuentes WHERE perfil_id = %s ORDER BY orden",
        perfil["id"],
    )
    assert [fila[0] for fila in linaje] == [PRODUCTOS, HISTORIAL, INGRESOS, REGISTROS]
    assert linaje[0][1] == [
        "antiguedad_productos_anios",
        "cuota_mensual_obligaciones",
        "entidades_con_deuda",
    ]
    assert [fila[3] for fila in linaje] == ["3.1", "3.1", "3.1", "1.8"]
    assert all(fila[4] is not None and fila[5] for fila in linaje)
    assert all(fila[1] == fila[2] for fila in linaje)


def test_mt_w10_02_c02_ingresos_no_autorizados_no_se_piden(cotizacion: Any) -> None:
    svc = cotizacion()
    perfil = svc.enviar("02").json()
    solicitudes = svc.wiremock.a_aliados(cliente_id("02"))
    assert len(solicitudes) == 3
    assert "aggregated-income" not in {_recurso(s) for s in solicitudes}
    assert _fuentes(perfil)[INGRESOS] == "NO_AUTORIZADA"
    relacion = next(f for f in perfil["factores"] if f["codigo"] == "RELACION_CUOTA_INGRESO")
    assert (relacion["estado"], relacion["motivo"]) == ("NO_EVALUADO", "NO_AUTORIZADO")
    assert (perfil["estado"], perfil["puntaje"], perfil["confianza"]) == ("CALCULADO", 75.0, 0.87)


def test_mt_w10_03_c03_dato_ausente_no_se_inventa(cotizacion: Any) -> None:
    svc = cotizacion()
    perfil = svc.enviar("03").json()
    assert perfil["estado"] == "INFORMACION_INSUFICIENTE"
    assert perfil["datosRequeridos"] == [
        {"campo": "moras_12m", "fuente": HISTORIAL, "motivo": "NO_DISPONIBLE_EN_FUENTE"}
    ]
    assert perfil["accionSugerida"] is None and perfil["nivelRiesgo"] is None
    ((insumos,),) = svc.bases.filas(
        "SELECT insumos FROM perfiles_riesgo WHERE id = %s", perfil["id"]
    )
    assert "moras_12m" not in insumos
    ((disponibles,),) = svc.bases.filas(
        "SELECT campos_disponibles FROM linaje_fuentes WHERE perfil_id = %s AND fuente = %s",
        perfil["id"],
        HISTORIAL,
    )
    assert disponibles == []


def test_mt_w10_04_c04_campos_extra_no_salen_del_adaptador(cotizacion: Any) -> None:
    svc = cotizacion()
    respuesta = svc.enviar("04")
    perfil = respuesta.json()
    assert (perfil["estado"], perfil["puntaje"]) == ("CALCULADO", 75.9)
    guardado = json.dumps(
        svc.bases.filas(
            "SELECT insumos, factores FROM perfiles_riesgo WHERE id = %s", perfil["id"]
        ),
        default=str,
    )
    linaje = json.dumps(
        svc.bases.filas("SELECT * FROM linaje_fuentes WHERE perfil_id = %s", perfil["id"]),
        default=str,
    )
    outbox = json.dumps(
        svc.bases.filas("SELECT payload FROM outbox WHERE aggregate_id = %s", perfil["id"]),
        default=str,
    )
    spans = " ".join(f"{s.name} {dict(s.attributes or {})}" for s in svc.lista_spans())
    for extra in (
        "SIN-CTA-0004",
        "Cliente Sintético 04",
        "accountNumbers",
        "fullName",
        "transactions",
    ):
        for texto in (respuesta.text, guardado, linaje, outbox, svc.logs.getvalue(), spans):
            assert extra not in texto


def test_mt_w10_05_c05_respuesta_invalida(cotizacion: Any) -> None:
    perfil = cotizacion().enviar("05").json()
    assert perfil["estado"] == "INFORMACION_INSUFICIENTE"
    assert perfil["datosRequeridos"] == [
        {"campo": "cuota_mensual_obligaciones", "fuente": PRODUCTOS, "motivo": "RESPUESTA_INVALIDA"}
    ]
    assert perfil["accionSugerida"] == "RECALCULAR"
    assert _fuentes(perfil)[PRODUCTOS] == "RESPUESTA_INVALIDA"


@pytest.mark.parametrize(
    ("nn", "causa"),
    [("08", "REVOCADO"), ("09", "VENCIDO"), ("0a", "SIN_CONSENTIMIENTO")],
    ids=["C08", "C09", "C10"],
)
def test_mt_w10_06_sin_consentimiento_no_se_invoca_a_nadie(
    cotizacion: Any, nn: str, causa: str
) -> None:
    svc = cotizacion()
    respuesta = svc.enviar(nn)
    assert respuesta.status_code == 201
    perfil = respuesta.json()
    assert perfil["estado"] == "INFORMACION_INSUFICIENTE"
    assert perfil["accionSugerida"] == "COMPLETAR_CONSENTIMIENTO"
    assert perfil["consentimiento"]["decision"] == "RECHAZADO"
    assert perfil["consentimiento"]["causa"] == causa
    assert set(_fuentes(perfil).values()) == {"NO_AUTORIZADA"}
    assert svc.wiremock.a_aliados(cliente_id(nn)) == []
    eventos = svc.bases.filas(
        "SELECT event_type FROM outbox WHERE aggregate_id = %s ORDER BY event_type", perfil["id"]
    )
    assert eventos == [("DatosConsentidosConsultados",), ("PerfilRiesgoCalculado",)]


@pytest.mark.parametrize(
    ("nn", "requeridos", "fuentes"),
    [
        (
            "10",
            [("moras_12m", HISTORIAL)],
            {
                PRODUCTOS: "NO_CONSULTADA",
                HISTORIAL: "NO_AUTORIZADA",
                INGRESOS: "NO_CONSULTADA",
                REGISTROS: "NO_CONSULTADA",
            },
        ),
        (
            "11",
            # Orden del modelo: declarados y luego campos por fuente (FB4; 01 §3.5 lista moras primero).
            [("cuota_mensual_obligaciones", PRODUCTOS), ("moras_12m", HISTORIAL)],
            {
                PRODUCTOS: "NO_AUTORIZADA",
                HISTORIAL: "NO_AUTORIZADA",
                INGRESOS: "NO_AUTORIZADA",
                REGISTROS: "NO_CONSULTADA",
            },
        ),
    ],
    ids=["C16", "C17"],
)
def test_mt_w10_07_minimizacion_sin_obligatorios_autorizados(
    cotizacion: Any, nn: str, requeridos: list[tuple[str, str]], fuentes: dict[str, str]
) -> None:
    svc = cotizacion()
    perfil = svc.enviar(nn).json()
    assert perfil["estado"] == "INFORMACION_INSUFICIENTE"
    assert perfil["accionSugerida"] == "COMPLETAR_CONSENTIMIENTO"
    assert [(r["campo"], r["fuente"]) for r in perfil["datosRequeridos"]] == requeridos
    assert {r["motivo"] for r in perfil["datosRequeridos"]} == {"NO_AUTORIZADO"}
    assert _fuentes(perfil) == fuentes
    assert svc.wiremock.a_aliados(cliente_id(nn)) == []
