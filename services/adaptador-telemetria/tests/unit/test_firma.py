"""UT-W21-01: firma HMAC-SHA256 y desfase de la marca de tiempo (HU-W21, DS3-16)."""

from datetime import UTC, datetime, timedelta

import pytest
from adaptador_telemetria.domain.telemetria.firma import (
    calcular_firma,
    dentro_de_desfase,
    leer_marca,
    verificar_firma,
)

SECRETO = "secreto-de-ejemplo"
MARCA = "2026-11-10T14:00:05Z"
CUERPO = (
    b'{"versionContrato":"1","idExterno":"IDEAM-2026-1110-0001","eventoOrigen":"EVT-2026-0913",'
    b'"tipoEvento":"LLUVIA_ACUMULADA","valorObservado":"135","unidad":"mm","zona":"11001",'
    b'"observadoEn":"2026-11-10T14:00:00Z"}'
)
# El ejemplo trabajado de docs/hu-w21-evento-parametrico.md §1.1: lo calculó otra implementación.
FIRMA = "sha256=2e718c810919919f16d77c37c48e5ebccd9e8ea9247839ff05c3d4eb3a7c8ff5"


def test_la_firma_es_la_del_ejemplo_de_la_guia() -> None:
    assert calcular_firma(SECRETO, MARCA, CUERPO) == FIRMA


def test_la_firma_valida_se_acepta() -> None:
    assert verificar_firma(SECRETO, MARCA, CUERPO, FIRMA)


@pytest.mark.parametrize("posicion", [0, 10, 100, -1])
def test_un_byte_alterado_del_cuerpo_se_rechaza(posicion: int) -> None:
    alterado = bytearray(CUERPO)
    alterado[posicion] ^= 0x01
    assert not verificar_firma(SECRETO, MARCA, bytes(alterado), FIRMA)


def test_un_cuerpo_reformateado_no_conserva_la_firma() -> None:
    """La firma es sobre los bytes recibidos: un espacio de más ya es otro cuerpo."""
    assert not verificar_firma(SECRETO, MARCA, CUERPO.replace(b",", b", ", 1), FIRMA)


def test_la_marca_de_tiempo_forma_parte_de_la_firma() -> None:
    assert not verificar_firma(SECRETO, "2026-11-10T14:00:06Z", CUERPO, FIRMA)
    assert not verificar_firma(SECRETO, MARCA.replace("Z", "+00:00"), CUERPO, FIRMA)


@pytest.mark.parametrize("posicion", [len("sha256="), -1])
def test_un_caracter_alterado_de_la_firma_se_rechaza(posicion: int) -> None:
    alterada = list(FIRMA)
    alterada[posicion] = "0" if FIRMA[posicion] != "0" else "1"
    assert not verificar_firma(SECRETO, MARCA, CUERPO, "".join(alterada))


def test_otro_secreto_se_rechaza() -> None:
    assert not verificar_firma("otro-secreto", MARCA, CUERPO, FIRMA)


@pytest.mark.parametrize(
    "firma", [None, "", "sha256=", FIRMA.removeprefix("sha256="), FIRMA.upper(), "ñandú"]
)
def test_una_firma_ausente_o_mal_formada_se_rechaza(firma: str | None) -> None:
    """Sin excepciones, ni siquiera con caracteres que no son ASCII."""
    assert not verificar_firma(SECRETO, MARCA, CUERPO, firma)


# --- Desfase -------------------------------------------------------------------------------------
AHORA = datetime(2026, 11, 10, 14, 0, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    ("segundos", "esperado"),
    [(0, True), (-299, True), (-300, True), (-301, False), (299, True), (300, True), (301, False)],
)
def test_desfase_en_ambos_sentidos(segundos: int, esperado: bool) -> None:
    marca = AHORA + timedelta(seconds=segundos)
    assert dentro_de_desfase(marca, AHORA, 300) is esperado


def test_el_desfase_cuenta_fracciones_de_segundo() -> None:
    assert not dentro_de_desfase(AHORA - timedelta(seconds=300, microseconds=1), AHORA, 300)
    assert dentro_de_desfase(AHORA - timedelta(seconds=300), AHORA, 300)


# --- Lectura de la marca -------------------------------------------------------------------------
@pytest.mark.parametrize(
    "texto",
    ["2026-11-10T14:00:05Z", "2026-11-10T09:00:05-05:00", "2026-11-10T14:00:05+00:00"],
)
def test_la_marca_se_lee_en_utc(texto: str) -> None:
    assert leer_marca(texto) == AHORA
    assert leer_marca(texto) is not None and leer_marca(texto).tzinfo == UTC  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "texto",
    [None, "", "ayer", "2026-11-10", "2026-11-10T14:00:05", "1731247205", "2026-13-40T00:00:00Z"],
)
def test_una_marca_ausente_ilegible_o_sin_zona_no_es_un_instante(texto: str | None) -> None:
    assert leer_marca(texto) is None
