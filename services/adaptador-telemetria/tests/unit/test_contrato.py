"""Contrato v1 del evento externo y la identidad del evento (HU-W21, DS3-16)."""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from adaptador_telemetria.domain.telemetria.contrato import validar_contrato
from adaptador_telemetria.domain.telemetria.errores import ContratoInvalido
from adaptador_telemetria.domain.telemetria.modelos import clave_archivo, evento_id


def _cuerpo(**cambios: Any) -> dict[str, Any]:
    cuerpo: dict[str, Any] = {
        "versionContrato": "1",
        "idExterno": "IDEAM-2026-1110-0001",
        "eventoOrigen": "EVT-2026-0913",
        "tipoEvento": "LLUVIA_ACUMULADA",
        "valorObservado": "135",
        "unidad": "mm",
        "zona": "11001",
        "observadoEn": "2026-11-10T14:00:00Z",
    }
    cuerpo.update(cambios)
    return {k: v for k, v in cuerpo.items() if v is not None}


def _validar(cuerpo: dict[str, Any] | bytes) -> Any:
    crudo = cuerpo if isinstance(cuerpo, bytes) else json.dumps(cuerpo).encode()
    return validar_contrato(crudo, "IDEAM-SIM")


def _campos(cuerpo: dict[str, Any] | bytes) -> tuple[str, ...]:
    with pytest.raises(ContratoInvalido) as error:
        _validar(cuerpo)
    return error.value.campos


def test_un_cuerpo_valido_da_el_evento_canonico() -> None:
    evento = _validar(_cuerpo(ubicacion={"lat": 4.711, "lon": -74.0721}))
    assert (evento.fuente, evento.id_externo, evento.evento_origen) == (
        "IDEAM-SIM",
        "IDEAM-2026-1110-0001",
        "EVT-2026-0913",
    )
    assert (evento.tipo_evento, evento.valor_observado, evento.unidad, evento.zona) == (
        "LLUVIA_ACUMULADA",
        "135",
        "mm",
        "11001",
    )
    assert evento.observado_en == datetime(2026, 11, 10, 14, 0, tzinfo=UTC)
    # La ubicación no entra al modelo canónico (minimización).
    assert not hasattr(evento, "ubicacion")


def test_el_retraso_de_vuelo_va_en_minutos() -> None:
    evento = _validar(
        _cuerpo(tipoEvento="RETRASO_VUELO", unidad="min", zona="AV204", valorObservado="180")
    )
    assert (evento.tipo_evento, evento.unidad) == ("RETRASO_VUELO", "min")


def test_el_instante_con_otra_zona_horaria_se_lleva_a_utc() -> None:
    evento = _validar(_cuerpo(observadoEn="2026-11-10T09:00:00-05:00"))
    assert evento.observado_en == datetime(2026, 11, 10, 14, 0, tzinfo=UTC)
    assert evento.observado_en.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("cambios", "campos"),
    [
        ({"versionContrato": "2"}, ("versionContrato",)),
        ({"versionContrato": 1}, ("versionContrato",)),
        ({"versionContrato": None}, ("versionContrato",)),
        ({"idExterno": None}, ("idExterno",)),
        ({"idExterno": "con espacio"}, ("idExterno",)),
        ({"idExterno": "x" * 65}, ("idExterno",)),
        ({"eventoOrigen": ""}, ("eventoOrigen",)),
        ({"tipoEvento": "GRANIZO"}, ("tipoEvento",)),
        ({"valorObservado": "mucha"}, ("valorObservado",)),
        ({"valorObservado": 135}, ("valorObservado",)),
        ({"valorObservado": "-1"}, ("valorObservado",)),
        ({"valorObservado": "135.1234"}, ("valorObservado",)),
        ({"valorObservado": "1e3"}, ("valorObservado",)),
        ({"valorObservado": "١٢٠"}, ("valorObservado",)),
        ({"unidad": "km"}, ("unidad",)),
        ({"unidad": "min"}, ("unidad",)),
        ({"zona": "bogota"}, ("zona",)),
        ({"zona": "1"}, ("zona",)),
        ({"observadoEn": "2026-11-10T14:00:00"}, ("observadoEn",)),
        ({"observadoEn": "ayer"}, ("observadoEn",)),
        ({"ubicacion": {"lat": 91, "lon": 0}}, ("ubicacion.lat",)),
        ({"ubicacion": {"lat": 4.7}}, ("ubicacion.lon",)),
        ({"contacto": "x@ejemplo.test"}, ("contacto",)),
        ({"id_externo": "IDEAM-2026-1110-0002"}, ("id_externo",)),
    ],
)
def test_cada_desvio_nombra_el_campo_sin_su_valor(
    cambios: dict[str, Any], campos: tuple[str, ...]
) -> None:
    cuerpo = _cuerpo(**cambios)
    resultado = _campos(cuerpo)
    # Un campo con el nombre equivocado deja además sin él al campo obligatorio.
    assert set(campos) <= set(resultado)
    assert "x@ejemplo.test" not in "".join(resultado)


def test_la_unidad_incoherente_con_el_tipo_se_rechaza_en_los_dos_sentidos() -> None:
    assert _campos(_cuerpo(tipoEvento="RETRASO_VUELO", unidad="mm", zona="AV204")) == ("unidad",)
    assert _campos(_cuerpo(tipoEvento="LLUVIA_ACUMULADA", unidad="min")) == ("unidad",)


@pytest.mark.parametrize(
    "crudo",
    [
        b"",
        b"no es json",
        b'{"versionContrato": "1", "idExterno": ',
        b"[]",
        b"null",
        b'"texto"',
        b"1",
    ],
)
def test_un_cuerpo_que_no_es_un_objeto_json_no_nombra_campos(crudo: bytes) -> None:
    assert _campos(crudo) == ()


def test_el_error_no_encadena_la_excepcion_con_los_valores_recibidos() -> None:
    with pytest.raises(ContratoInvalido) as error:
        _validar(_cuerpo(valorObservado="valor-secreto"))
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__
    assert "valor-secreto" not in str(error.value)
    assert "valor-secreto" not in repr(error.value.campos)


# --- Identidad -----------------------------------------------------------------------------------
def test_el_evento_id_es_el_uuid5_de_la_fuente_y_el_id_externo() -> None:
    # Del ejemplo de docs/hu-w21-evento-parametrico.md §1.1.
    assert evento_id("IDEAM-SIM", "IDEAM-2026-1110-0001") == UUID(
        "bafc2b26-7c88-5ae3-b5ac-26fdf5427a09"
    )
    assert evento_id("IDEAM-SIM", "IDEAM-2026-1110-0001") == evento_id(
        "IDEAM-SIM", "IDEAM-2026-1110-0001"
    )


def test_otra_fuente_u_otro_id_externo_es_otro_evento() -> None:
    base = evento_id("IDEAM-SIM", "A-1")
    assert evento_id("AEROCIVIL-SIM", "A-1") != base
    assert evento_id("IDEAM-SIM", "A-2") != base


def test_el_evento_canonico_conoce_su_id() -> None:
    evento = _validar(_cuerpo())
    assert evento.evento_id == evento_id("IDEAM-SIM", "IDEAM-2026-1110-0001")


def test_el_id_no_depende_del_formato_del_cuerpo() -> None:
    a = validar_contrato(json.dumps(_cuerpo(), indent=2).encode(), "IDEAM-SIM")
    b = validar_contrato(json.dumps(_cuerpo(valorObservado="136")).encode(), "IDEAM-SIM")
    assert a.evento_id == b.evento_id


def test_la_clave_del_archivo_usa_la_fecha_utc_de_la_recepcion() -> None:
    evento = UUID("bafc2b26-7c88-5ae3-b5ac-26fdf5427a09")
    # 23:30 en Colombia del 9 de nov ya es el 10 de nov en UTC.
    recibido = datetime.fromisoformat("2026-11-09T23:30:00-05:00")
    assert clave_archivo("telemetria/", recibido, evento) == (
        "telemetria/2026/11/10/bafc2b26-7c88-5ae3-b5ac-26fdf5427a09.json"
    )


def test_un_nombre_ajeno_dentro_de_la_ubicacion_tambien_se_rechaza() -> None:
    assert _campos(_cuerpo(ubicacion={"lat": 4.7, "lon": -74.0, "altura": 2600})) == (
        "ubicacion.altura",
    )


def test_los_nombres_de_campo_de_mas_van_acotados() -> None:
    cuerpo = _cuerpo(**{f"campo{i}" + "x" * 100: i for i in range(40)})
    campos = _campos(cuerpo)
    assert len(campos) == 20
    assert all(len(campo) <= 64 for campo in campos)
