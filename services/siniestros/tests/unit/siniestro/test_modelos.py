"""Invariantes del siniestro y de la evidencia (HU-W19, DS3-02)."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from siniestros.domain.siniestro.errores import SiniestroInvalido
from siniestros.domain.siniestro.modelos import (
    EstadoSiniestro,
    Evidencia,
    Siniestro,
    TipoEvidencia,
    TipoSoporte,
    Ubicacion,
)

ANALISTA = uuid4()


def test_un_reportado_valido_no_tiene_analista(siniestro: Callable[..., Siniestro]) -> None:
    caso = siniestro()
    assert caso.estado is EstadoSiniestro.REPORTADO
    assert caso.analista_id is None
    assert not caso.reloj_pausado


@pytest.mark.parametrize(
    "codigo", ["", "88-26-0442", "99-2026-0442", "88-2026-044", "88-2026-04a2"]
)
def test_el_codigo_tiene_la_forma_88_anio_numero(
    siniestro: Callable[..., Siniestro], codigo: str
) -> None:
    with pytest.raises(SiniestroInvalido, match="codigo"):
        siniestro(codigo=codigo)


def test_el_codigo_admite_mas_de_cuatro_digitos(siniestro: Callable[..., Siniestro]) -> None:
    assert siniestro(codigo="88-2026-12345").codigo == "88-2026-12345"


@pytest.mark.parametrize(
    "campo", ["fecha_evento", "reportado_en", "asignado_en", "plazo_soporte_vence_en"]
)
def test_los_instantes_exigen_zona(
    siniestro: Callable[..., Siniestro], ingenuo: datetime, campo: str
) -> None:
    with pytest.raises(SiniestroInvalido, match=campo):
        siniestro(**{campo: ingenuo})


def test_la_pausa_acumulada_no_es_negativa(siniestro: Callable[..., Siniestro]) -> None:
    with pytest.raises(SiniestroInvalido, match="pausa_acumulada_s"):
        siniestro(pausa_acumulada_s=-1)


@pytest.mark.parametrize("estado", [EstadoSiniestro.EN_EVALUACION, EstadoSiniestro.FALTA_SOPORTE])
def test_un_caso_asignado_exige_analista(
    siniestro: Callable[..., Siniestro], ahora: datetime, estado: EstadoSiniestro
) -> None:
    extra = {"pausado_desde": ahora} if estado is EstadoSiniestro.FALTA_SOPORTE else {}
    with pytest.raises(SiniestroInvalido, match="analista_id"):
        siniestro(estado=estado, **extra)


def test_un_reportado_no_puede_tener_analista(siniestro: Callable[..., Siniestro]) -> None:
    with pytest.raises(SiniestroInvalido, match="analista_id"):
        siniestro(analista_id=ANALISTA)


def test_un_caso_decidido_puede_no_tener_analista(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    # Lo aprueba un paramétrico (W22).
    caso = siniestro(estado=EstadoSiniestro.APROBADO, decidido_en=ahora)
    assert caso.analista_id is None


def test_el_reloj_solo_se_pausa_en_falta_de_soporte(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    pausado = siniestro(
        estado=EstadoSiniestro.FALTA_SOPORTE, analista_id=ANALISTA, pausado_desde=ahora
    )
    assert pausado.reloj_pausado
    with pytest.raises(SiniestroInvalido, match="pausado_desde"):
        siniestro(estado=EstadoSiniestro.EN_EVALUACION, analista_id=ANALISTA, pausado_desde=ahora)
    with pytest.raises(SiniestroInvalido, match="pausado_desde"):
        siniestro(estado=EstadoSiniestro.FALTA_SOPORTE, analista_id=ANALISTA)


def test_solo_un_caso_decidido_tiene_fecha_de_decision(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    with pytest.raises(SiniestroInvalido, match="decidido_en"):
        siniestro(estado=EstadoSiniestro.RECHAZADO)
    with pytest.raises(SiniestroInvalido, match="decidido_en"):
        siniestro(decidido_en=ahora)


def test_el_siniestro_es_inmutable(siniestro: Callable[..., Siniestro]) -> None:
    caso = siniestro()
    with pytest.raises(AttributeError):
        caso.estado = EstadoSiniestro.APROBADO  # type: ignore[misc]


def test_una_ubicacion_con_coordenadas_las_tiene_las_dos() -> None:
    assert Ubicacion(lat=4.6, lon=-74.0).tiene_coordenadas
    assert not Ubicacion(lat=4.6).tiene_coordenadas
    assert not Ubicacion(descripcion="Bogotá").tiene_coordenadas


def test_un_soporte_lleva_su_tipo_y_una_foto_no(evidencia: Callable[..., Evidencia]) -> None:
    assert evidencia(tipo=TipoEvidencia.SOPORTE, soporte_tipo=TipoSoporte.PERITAJE).soporte_tipo
    with pytest.raises(SiniestroInvalido, match="soporte_tipo"):
        evidencia(tipo=TipoEvidencia.SOPORTE)
    with pytest.raises(SiniestroInvalido, match="soporte_tipo"):
        evidencia(soporte_tipo=TipoSoporte.PERITAJE)


@pytest.mark.parametrize("sha", ["", "A" * 64, "a" * 63, "g" * 64])
def test_el_sha256_son_64_hexadecimales_en_minuscula(
    evidencia: Callable[..., Evidencia], sha: str
) -> None:
    with pytest.raises(SiniestroInvalido, match="sha256"):
        evidencia(sha256=sha)


def test_la_captura_exige_zona(evidencia: Callable[..., Evidencia], ingenuo: datetime) -> None:
    with pytest.raises(SiniestroInvalido, match="capturada_en"):
        evidencia(capturada_en=ingenuo)
    assert evidencia(capturada_en=None).capturada_en is None


def test_la_representacion_no_filtra_la_clave_ni_el_hash(
    evidencia: Callable[..., Evidencia],
) -> None:
    archivo = evidencia(capturada_en=datetime.now(UTC) - timedelta(days=1))
    texto = repr(archivo)
    assert archivo.clave_objeto not in texto
    assert archivo.sha256 not in texto
