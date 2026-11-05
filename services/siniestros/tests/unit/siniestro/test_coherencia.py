"""Coherencia de la evidencia (DS3-06)."""

from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID

import pytest
from siniestros.domain.siniestro.coherencia import (
    Declarado,
    distancia_m,
    evaluar_coherencia,
)
from siniestros.domain.siniestro.modelos import (
    Evidencia,
    IntegridadEvidencia,
    ResultadoCoherencia,
    Ubicacion,
)

BOGOTA = Ubicacion(descripcion="Bogotá D.C.", lat=4.652, lon=-74.0555)
CERCA = Ubicacion(lat=4.6512, lon=-74.056)


@pytest.fixture
def declarado(ahora: datetime) -> Declarado:
    return Declarado(ahora - timedelta(hours=14, minutes=41), BOGOTA)


def test_una_captura_posterior_y_cercana_es_coherente(
    declarado: Declarado, evidencia: Callable[..., Evidencia]
) -> None:
    archivo = evidencia(
        capturada_en=declarado.fecha_evento + timedelta(minutes=34), ubicacion=CERCA
    )
    resultado = evaluar_coherencia(
        declarado, [archivo], {archivo.id: IntegridadEvidencia.VERIFICADA}
    )
    assert resultado.resultado is ResultadoCoherencia.COHERENTE
    assert resultado.diferencia_min == 34
    assert resultado.primera_captura == archivo.capturada_en
    assert resultado.fecha_declarada == declarado.fecha_evento
    assert resultado.ubicacion_coincide is True
    assert resultado.archivos_alterados == 0


def test_la_primera_captura_es_la_menor(
    declarado: Declarado, evidencia: Callable[..., Evidencia]
) -> None:
    tarde = evidencia(capturada_en=declarado.fecha_evento + timedelta(minutes=50))
    temprano = evidencia(capturada_en=declarado.fecha_evento + timedelta(minutes=10))
    sin_fecha = evidencia(capturada_en=None)
    resultado = evaluar_coherencia(declarado, [tarde, sin_fecha, temprano], {})
    assert resultado.primera_captura == temprano.capturada_en
    assert resultado.diferencia_min == 10


def test_una_captura_anterior_al_evento_es_inconsistente(
    declarado: Declarado, evidencia: Callable[..., Evidencia]
) -> None:
    archivo = evidencia(capturada_en=declarado.fecha_evento - timedelta(minutes=5))
    resultado = evaluar_coherencia(declarado, [archivo], {})
    assert resultado.diferencia_min == -5
    assert resultado.resultado is ResultadoCoherencia.INCONSISTENTE


def test_una_captura_unos_segundos_antes_ya_es_inconsistente(
    declarado: Declarado, evidencia: Callable[..., Evidencia]
) -> None:
    archivo = evidencia(capturada_en=declarado.fecha_evento - timedelta(seconds=10))
    resultado = evaluar_coherencia(declarado, [archivo], {})
    assert resultado.diferencia_min == -1
    assert resultado.resultado is ResultadoCoherencia.INCONSISTENTE


def test_una_captura_justo_al_momento_del_evento_es_coherente(
    declarado: Declarado, evidencia: Callable[..., Evidencia]
) -> None:
    archivo = evidencia(capturada_en=declarado.fecha_evento, ubicacion=CERCA)
    resultado = evaluar_coherencia(declarado, [archivo], {})
    assert resultado.diferencia_min == 0
    assert resultado.resultado is ResultadoCoherencia.COHERENTE


def test_una_ubicacion_lejana_es_inconsistente(
    declarado: Declarado, evidencia: Callable[..., Evidencia]
) -> None:
    medellin = Ubicacion(lat=6.2442, lon=-75.5812)
    archivo = evidencia(
        capturada_en=declarado.fecha_evento + timedelta(minutes=5), ubicacion=medellin
    )
    resultado = evaluar_coherencia(declarado, [archivo], {})
    assert resultado.ubicacion_coincide is False
    assert resultado.resultado is ResultadoCoherencia.INCONSISTENTE


def test_el_umbral_de_distancia_es_configurable(
    declarado: Declarado, evidencia: Callable[..., Evidencia]
) -> None:
    # CERCA está a unos 90 m de BOGOTA.
    archivo = evidencia(capturada_en=declarado.fecha_evento + timedelta(minutes=5), ubicacion=CERCA)
    assert evaluar_coherencia(declarado, [archivo], {}).ubicacion_coincide is True
    estricto = evaluar_coherencia(declarado, [archivo], {}, distancia_max_m=10)
    assert estricto.ubicacion_coincide is False


def test_la_ubicacion_usa_la_primera_evidencia_con_coordenadas(
    declarado: Declarado, evidencia: Callable[..., Evidencia]
) -> None:
    lejos = Ubicacion(lat=6.2442, lon=-75.5812)
    escaneo = evidencia(
        capturada_en=declarado.fecha_evento + timedelta(minutes=1),
        ubicacion=Ubicacion(descripcion="Escaneado"),
    )
    foto = evidencia(capturada_en=declarado.fecha_evento + timedelta(minutes=2), ubicacion=CERCA)
    posterior = evidencia(
        capturada_en=declarado.fecha_evento + timedelta(minutes=9), ubicacion=lejos
    )
    resultado = evaluar_coherencia(declarado, [posterior, escaneo, foto], {})
    assert resultado.ubicacion_coincide is True


def test_sin_coordenadas_en_lo_declarado_no_se_sabe(
    ahora: datetime, evidencia: Callable[..., Evidencia]
) -> None:
    declarado = Declarado(ahora - timedelta(hours=1), Ubicacion(descripcion="Bogotá"))
    archivo = evidencia(capturada_en=ahora - timedelta(minutes=30), ubicacion=CERCA)
    resultado = evaluar_coherencia(declarado, [archivo], {})
    assert resultado.ubicacion_coincide is None
    assert resultado.resultado is ResultadoCoherencia.COHERENTE


def test_sin_coordenadas_en_la_evidencia_no_se_sabe(
    declarado: Declarado, evidencia: Callable[..., Evidencia]
) -> None:
    archivo = evidencia(capturada_en=declarado.fecha_evento + timedelta(minutes=5), ubicacion=None)
    assert evaluar_coherencia(declarado, [archivo], {}).ubicacion_coincide is None


def test_un_archivo_alterado_vuelve_inconsistente_el_caso(
    declarado: Declarado, evidencia: Callable[..., Evidencia]
) -> None:
    buena = evidencia(capturada_en=declarado.fecha_evento + timedelta(minutes=5))
    alterada = evidencia(capturada_en=declarado.fecha_evento + timedelta(minutes=6))
    integridades: dict[UUID, IntegridadEvidencia] = {
        buena.id: IntegridadEvidencia.VERIFICADA,
        alterada.id: IntegridadEvidencia.ALTERADA,
    }
    resultado = evaluar_coherencia(declarado, [buena, alterada], integridades)
    assert resultado.archivos_alterados == 1
    assert resultado.resultado is ResultadoCoherencia.INCONSISTENTE


@pytest.mark.parametrize(
    "integridad", [IntegridadEvidencia.NO_DISPONIBLE, IntegridadEvidencia.NO_VERIFICABLE]
)
def test_un_objeto_no_disponible_o_no_verificable_no_vuelve_inconsistente(
    declarado: Declarado, evidencia: Callable[..., Evidencia], integridad: IntegridadEvidencia
) -> None:
    archivo = evidencia(capturada_en=declarado.fecha_evento + timedelta(minutes=5))
    resultado = evaluar_coherencia(declarado, [archivo], {archivo.id: integridad})
    assert resultado.archivos_alterados == 0
    assert resultado.resultado is ResultadoCoherencia.COHERENTE


def test_sin_ninguna_captura_con_fecha_no_es_evaluable(
    declarado: Declarado, evidencia: Callable[..., Evidencia]
) -> None:
    sin_fecha = evidencia(capturada_en=None, ubicacion=None)
    for archivos in ([], [sin_fecha]):
        resultado = evaluar_coherencia(declarado, archivos, {})
        assert resultado.resultado is ResultadoCoherencia.NO_EVALUABLE
        assert resultado.primera_captura is None
        assert resultado.diferencia_min is None


def test_un_alterado_sin_fecha_de_captura_sigue_siendo_inconsistente(
    declarado: Declarado, evidencia: Callable[..., Evidencia]
) -> None:
    archivo = evidencia(capturada_en=None, ubicacion=None)
    resultado = evaluar_coherencia(declarado, [archivo], {archivo.id: IntegridadEvidencia.ALTERADA})
    assert resultado.resultado is ResultadoCoherencia.INCONSISTENTE


def test_la_distancia_de_un_grado_de_latitud_son_unos_111_km() -> None:
    uno = distancia_m(Ubicacion(lat=4.0, lon=-74.0), Ubicacion(lat=5.0, lon=-74.0))
    assert uno == pytest.approx(111_195, rel=0.001)


def test_la_distancia_al_mismo_punto_es_cero_y_es_simetrica() -> None:
    assert distancia_m(BOGOTA, BOGOTA) == 0
    assert distancia_m(BOGOTA, CERCA) == pytest.approx(distancia_m(CERCA, BOGOTA))


@pytest.mark.parametrize(
    "ubicacion", [Ubicacion(), Ubicacion(lat=4.0), Ubicacion(lon=-74.0), Ubicacion(descripcion="x")]
)
def test_la_distancia_exige_coordenadas(ubicacion: Ubicacion) -> None:
    with pytest.raises(ValueError, match="coordenadas"):
        distancia_m(ubicacion, BOGOTA)
    with pytest.raises(ValueError, match="coordenadas"):
        distancia_m(BOGOTA, ubicacion)
