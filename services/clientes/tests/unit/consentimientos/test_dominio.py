"""Dominio del consentimiento: invariantes, vigencia con el límite exacto y huella (ADR 0009, 0010)."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from clientes.domain.consentimientos.modelos import (
    ALCANCES,
    Consentimiento,
    EstadoConsentimiento,
    Hito,
    SolicitudInvalida,
    SolicitudOtorgamiento,
    TipoHito,
    VigenciaInvalida,
    esta_vigente,
)

HASTA = datetime(2027, 10, 17, 12, tzinfo=UTC)
UN_SEGUNDO = timedelta(seconds=1)


def _consentimiento(estado: EstadoConsentimiento, leido_en: datetime) -> Consentimiento:
    return Consentimiento(
        id=uuid4(),
        cliente_id=uuid4(),
        proposito="PERFILAMIENTO_PRECIO",
        alcances=("FA_PRODUCTOS_VIGENTES",),
        version_texto="v1",
        idioma="es-CO",
        estado=estado,
        otorgado_en=HASTA - timedelta(days=365),
        vigente_hasta=HASTA,
        revocado_en=HASTA - timedelta(days=1) if estado is EstadoConsentimiento.REVOCADO else None,
        leido_en=leido_en,
    )


def _solicitud(**cambios: object) -> SolicitudOtorgamiento:
    datos: dict[str, object] = {
        "proposito": "PERFILAMIENTO_PRECIO",
        "alcances": ["FA_PRODUCTOS_VIGENTES"],
        "vigencia_meses": 12,
        "version_texto": "v1",
        "idioma": "es-CO",
    } | cambios
    return SolicitudOtorgamiento.desde(**datos)  # type: ignore[arg-type]


def test_esta_vigente_un_instante_antes_del_limite() -> None:
    assert esta_vigente(EstadoConsentimiento.ACTIVO, HASTA, HASTA - UN_SEGUNDO)


def test_esta_vigente_en_el_limite_exacto_ya_no_lo_esta() -> None:
    assert not esta_vigente(EstadoConsentimiento.ACTIVO, HASTA, HASTA)


def test_esta_vigente_un_instante_despues_del_limite() -> None:
    assert not esta_vigente(EstadoConsentimiento.ACTIVO, HASTA, HASTA + UN_SEGUNDO)


@pytest.mark.parametrize("estado", [EstadoConsentimiento.REVOCADO, EstadoConsentimiento.VENCIDO])
def test_un_estado_final_nunca_esta_vigente(estado: EstadoConsentimiento) -> None:
    assert not esta_vigente(estado, HASTA, HASTA - timedelta(days=30))


def test_el_estado_derivado_de_un_activo_vigente_es_activo() -> None:
    c = _consentimiento(EstadoConsentimiento.ACTIVO, HASTA - UN_SEGUNDO)
    assert c.estado_derivado is EstadoConsentimiento.ACTIVO
    assert c.vigente


def test_el_estado_derivado_de_un_activo_vencido_sin_materializar_es_vencido() -> None:
    c = _consentimiento(EstadoConsentimiento.ACTIVO, HASTA)
    assert c.estado is EstadoConsentimiento.ACTIVO
    assert c.estado_derivado is EstadoConsentimiento.VENCIDO
    assert not c.vigente


def test_el_estado_derivado_de_un_revocado_se_conserva() -> None:
    c = _consentimiento(EstadoConsentimiento.REVOCADO, HASTA - UN_SEGUNDO)
    assert c.estado_derivado is EstadoConsentimiento.REVOCADO


def test_el_historial_de_un_activo_vigente_solo_tiene_el_otorgamiento() -> None:
    c = _consentimiento(EstadoConsentimiento.ACTIVO, HASTA - UN_SEGUNDO)
    assert c.historial == (Hito(TipoHito.OTORGADO, c.otorgado_en),)


def test_el_historial_de_un_revocado_tiene_otorgado_y_revocado() -> None:
    """HU-M09 AC4: cuándo se otorgó y cuándo se revocó, derivado de la fila."""
    c = _consentimiento(EstadoConsentimiento.REVOCADO, HASTA + UN_SEGUNDO)
    assert c.revocado_en is not None
    assert c.historial == (
        Hito(TipoHito.OTORGADO, c.otorgado_en),
        Hito(TipoHito.REVOCADO, c.revocado_en),
    )


@pytest.mark.parametrize(
    ("estado", "leido_en"),
    [
        (EstadoConsentimiento.ACTIVO, HASTA),  # vencido sin materializar, en el límite exacto
        (EstadoConsentimiento.VENCIDO, HASTA - UN_SEGUNDO),  # materializado al reotorgar
    ],
)
def test_el_historial_de_un_vencido_termina_en_vigente_hasta(
    estado: EstadoConsentimiento, leido_en: datetime
) -> None:
    c = _consentimiento(estado, leido_en)
    assert c.historial == (
        Hito(TipoHito.OTORGADO, c.otorgado_en),
        Hito(TipoHito.VENCIDO, HASTA),
    )


def test_solicitud_valida_se_normaliza() -> None:
    s = _solicitud(
        proposito=" perfilamiento_precio ",
        alcances=["da_registros_publicos", "FA_PRODUCTOS_VIGENTES", " fa_productos_vigentes"],
        version_texto=" v1 ",
        idioma=" en-US ",
    )
    assert s.proposito == "PERFILAMIENTO_PRECIO"
    assert s.alcances == ("DA_REGISTROS_PUBLICOS", "FA_PRODUCTOS_VIGENTES")
    assert (s.version_texto, s.idioma, s.vigencia_meses) == ("v1", "en-US", 12)


@pytest.mark.parametrize("meses", [6, 12])
def test_vigencias_permitidas(meses: int) -> None:
    assert _solicitud(vigencia_meses=meses).vigencia_meses == meses


@pytest.mark.parametrize(
    ("cambio", "campos"),
    [
        ({"proposito": None}, ["proposito"]),
        ({"proposito": "   "}, ["proposito"]),
        ({"proposito": "OTRO"}, ["proposito"]),
        ({"alcances": None}, ["alcances"]),
        ({"alcances": []}, ["alcances"]),
        ({"alcances": ["FA_INEXISTENTE"]}, ["alcances"]),
        ({"alcances": ["FA_PRODUCTOS_VIGENTES", ""]}, ["alcances"]),
        ({"vigencia_meses": None}, ["vigenciaMeses"]),
        ({"version_texto": None}, ["versionTexto"]),
        ({"version_texto": " "}, ["versionTexto"]),
        ({"idioma": None}, ["idioma"]),
        ({"idioma": "fr-FR"}, ["idioma"]),
        (
            {"proposito": None, "alcances": [], "vigencia_meses": None, "idioma": "xx"},
            ["proposito", "alcances", "vigenciaMeses", "idioma"],
        ),
    ],
)
def test_solicitud_incompleta_informa_los_campos(
    cambio: dict[str, object], campos: list[str]
) -> None:
    """AC3: se explica qué falta, sin crear nada."""
    with pytest.raises(SolicitudInvalida) as error:
        _solicitud(**cambio)
    assert list(error.value.campos) == campos


@pytest.mark.parametrize("meses", [0, 1, 7, 13, 24, -12])
def test_vigencia_fuera_de_la_lista_es_vigencia_invalida(meses: int) -> None:
    with pytest.raises(VigenciaInvalida):
        _solicitud(vigencia_meses=meses)


def test_un_campo_faltante_gana_sobre_una_vigencia_invalida() -> None:
    with pytest.raises(SolicitudInvalida):
        _solicitud(alcances=[], vigencia_meses=7)


def test_la_huella_ignora_orden_duplicados_espacios_y_mayusculas() -> None:
    a = _solicitud(alcances=["FA_INGRESOS_AGREGADOS", "DA_REGISTROS_PUBLICOS"])
    b = _solicitud(
        alcances=[" da_registros_publicos", "FA_INGRESOS_AGREGADOS", "fa_ingresos_agregados"],
        proposito="perfilamiento_precio ",
        version_texto=" v1",
    )
    assert a.huella() == b.huella()
    assert len(a.huella()) == 64


@pytest.mark.parametrize(
    "cambio",
    [
        {"alcances": ["FA_INGRESOS_AGREGADOS"]},
        {"vigencia_meses": 6},
        {"version_texto": "v2"},
        {"idioma": "en-US"},
    ],
)
def test_la_huella_cambia_si_cambia_el_cuerpo(cambio: dict[str, object]) -> None:
    assert _solicitud().huella() != _solicitud(**cambio).huella()


def test_los_alcances_son_los_cuatro_de_open_finance_y_open_data() -> None:
    assert ALCANCES == {
        "FA_PRODUCTOS_VIGENTES",
        "FA_HISTORIAL_PAGOS_12M",
        "FA_INGRESOS_AGREGADOS",
        "DA_REGISTROS_PUBLICOS",
    }
