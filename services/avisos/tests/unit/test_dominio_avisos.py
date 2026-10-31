from datetime import UTC, datetime
from uuid import uuid4

import pytest
from avisos.domain.avisos.modelos import (
    Aviso,
    Dispositivo,
    Entrega,
    EstadoDispositivo,
    PoliticaReintento,
    aviso_de_poliza_emitida,
    es_codigo,
    hash_token,
)

AHORA = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def test_el_aviso_solo_lleva_datos_y_ningun_dato_de_la_poliza() -> None:
    event_id, poliza_id = uuid4(), uuid4()

    aviso = aviso_de_poliza_emitida(event_id, poliza_id)

    assert aviso == Aviso("POLIZA_EMITIDA", poliza_id, event_id)
    assert {campo for campo in Aviso.__slots__} == {"tipo", "poliza_id", "event_id"}
    assert aviso.clave_colapso == str(event_id)


def test_hash_token_es_sha256_estable_y_no_contiene_el_token() -> None:
    huella = hash_token("token-sintetico-entregado")

    assert huella == hash_token("token-sintetico-entregado")
    assert len(huella) == 64
    assert "token" not in huella
    assert huella != hash_token("otro")


def test_invalidar_es_terminal_e_idempotente() -> None:
    dispositivo = Dispositivo(uuid4(), uuid4(), hash_token("t"))

    invalido = dispositivo.invalidar(AHORA, "TOKEN_RECHAZADO")

    assert invalido.estado is EstadoDispositivo.INVALIDO
    assert (invalido.invalidado_en, invalido.motivo_invalidez) == (AHORA, "TOKEN_RECHAZADO")
    assert invalido.invalidar(datetime(2030, 1, 1, tzinfo=UTC), "OTRO") is invalido
    assert dispositivo.estado is EstadoDispositivo.ACTIVO


@pytest.mark.parametrize("motivo", ["texto libre", "", "minuscula", "X" * 41])
def test_el_motivo_debe_ser_un_codigo(motivo: str) -> None:
    with pytest.raises(ValueError, match="código"):
        Dispositivo(uuid4(), uuid4(), hash_token("t")).invalidar(AHORA, motivo)
    assert not es_codigo(motivo)


def test_la_espera_crece_hasta_el_tope_y_se_agota() -> None:
    politica = PoliticaReintento(max_intentos=5, espera_inicial_s=5, espera_max_s=15)

    esperas = [(politica.proximo_intento(n, AHORA) or AHORA) - AHORA for n in (1, 2, 3, 4)]

    assert [e.total_seconds() for e in esperas] == [5, 10, 15, 15]
    assert politica.proximo_intento(5, AHORA) is None
    assert politica.proximo_intento(6, AHORA) is None


def test_el_repr_de_la_entrega_no_muestra_el_token() -> None:
    entrega = Entrega(
        uuid4(), uuid4(), aviso_de_poliza_emitida(uuid4(), uuid4()), 1, True, "token-secreto"
    )

    assert "token-secreto" not in repr(entrega)
    assert "token-secreto" not in str(entrega)
