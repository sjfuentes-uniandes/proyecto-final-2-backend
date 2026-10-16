import base64
import json

import pytest
from solventa_common import token_registro as tr

SECRETO = "secreto-sintetico-de-prueba-0123456789"
REGISTRO = "11111111-1111-4111-8111-111111111111"
CLIENTE = "22222222-2222-4222-8222-222222222222"
CORREO = "Persona@Ejemplo.co"
AHORA = 1_800_000_000


def _emitir(**cambios: object) -> str:
    argumentos: dict = {"ahora": AHORA}
    argumentos.update(cambios)
    token, _ = tr.emitir(SECRETO, REGISTRO, CLIENTE, CORREO, **argumentos)
    return token


def test_emitir_y_verificar_ida_y_vuelta() -> None:
    token, expira_en = tr.emitir(SECRETO, REGISTRO, CLIENTE, CORREO, ahora=AHORA)

    claims = tr.verificar(SECRETO, token, ahora=AHORA + 1)

    assert expira_en == AHORA + tr.TTL_S == AHORA + 1800
    assert claims == tr.ClaimsRegistro(REGISTRO, CLIENTE, "persona@ejemplo.co", expira_en)


def test_el_correo_se_compara_sin_importar_mayusculas_ni_espacios() -> None:
    token = _emitir()

    assert tr.verificar(SECRETO, token, correo="  PERSONA@ejemplo.CO ", ahora=AHORA)


def test_el_token_no_sirve_con_otro_correo() -> None:
    with pytest.raises(tr.TokenInvalido) as error:
        tr.verificar(SECRETO, _emitir(), correo="otra@ejemplo.co", ahora=AHORA)

    assert error.value.motivo == "CORREO"


def test_el_token_no_sirve_con_otro_registro() -> None:
    with pytest.raises(tr.TokenInvalido) as error:
        tr.verificar(SECRETO, _emitir(), registro_id="otro", ahora=AHORA)

    assert error.value.motivo == "REGISTRO"


def test_un_token_vencido_se_rechaza_justo_en_el_limite() -> None:
    token = _emitir(ttl_s=60)

    assert tr.verificar(SECRETO, token, ahora=AHORA + 59)
    with pytest.raises(tr.TokenInvalido) as error:
        tr.verificar(SECRETO, token, ahora=AHORA + 60)
    assert error.value.motivo == "VENCIDO"


def test_la_gracia_acepta_un_token_vencido_solo_hasta_su_limite() -> None:
    token = _emitir(ttl_s=60)

    assert tr.verificar(SECRETO, token, ahora=AHORA + 3000, gracia_s=3600)
    with pytest.raises(tr.TokenInvalido) as error:
        tr.verificar(SECRETO, token, ahora=AHORA + 3660, gracia_s=3600)
    assert error.value.motivo == "VENCIDO"


def test_otro_secreto_se_rechaza() -> None:
    with pytest.raises(tr.TokenInvalido) as error:
        tr.verificar("otro-secreto-sintetico-de-prueba-0123456789", _emitir(), ahora=AHORA)

    assert error.value.motivo == "FIRMA"


def _con_carga(**cambios: object) -> str:
    """Token con la carga alterada pero firmado de nuevo con el secreto correcto."""
    carga = {"rid": REGISTRO, "cid": CLIENTE, "eml": "a@b.co", "exp": AHORA + 100, **cambios}
    payload = tr._b64(json.dumps(carga).encode())
    mensaje = f"{tr.VERSION}.{payload}"
    return f"{mensaje}.{tr._firma(SECRETO, mensaje)}"


def test_alterar_la_carga_invalida_la_firma() -> None:
    version, payload, firma = _emitir().split(".")
    carga = json.loads(base64.urlsafe_b64decode(payload + "=="))
    carga["eml"] = "atacante@ejemplo.co"
    alterado = tr._b64(json.dumps(carga).encode())

    with pytest.raises(tr.TokenInvalido) as error:
        tr.verificar(SECRETO, f"{version}.{alterado}.{firma}", ahora=AHORA)

    assert error.value.motivo == "FIRMA"


@pytest.mark.parametrize(
    "token",
    ["", "x", "a.b", "v2.a.b", "v1.a.b.c", "v1..", None, 7],
)
def test_formato_incorrecto(token: object) -> None:
    with pytest.raises(tr.TokenInvalido) as error:
        tr.verificar(SECRETO, token, ahora=AHORA)  # type: ignore[arg-type]

    assert error.value.motivo in {"FORMATO", "FIRMA"}


@pytest.mark.parametrize(
    "cambios",
    [
        {"rid": 1},
        {"cid": None},
        {"eml": ["a"]},
        {"exp": "mañana"},
        {"exp": True},
        {"exp": 1.5},
    ],
)
def test_claims_con_tipos_incorrectos(cambios: dict) -> None:
    with pytest.raises(tr.TokenInvalido) as error:
        tr.verificar(SECRETO, _con_carga(**cambios), ahora=AHORA)

    assert error.value.motivo == "FORMATO"


def test_carga_que_no_es_json_o_sin_claims() -> None:
    for payload in (tr._b64(b"no-es-json"), tr._b64(b"{}"), tr._b64(b"[]")):
        mensaje = f"{tr.VERSION}.{payload}"
        token = f"{mensaje}.{tr._firma(SECRETO, mensaje)}"
        with pytest.raises(tr.TokenInvalido) as error:
            tr.verificar(SECRETO, token, ahora=AHORA)
        assert error.value.motivo == "FORMATO"


def test_un_secreto_corto_no_se_acepta() -> None:
    with pytest.raises(ValueError):
        tr.emitir("corto", REGISTRO, CLIENTE, CORREO)
    with pytest.raises(ValueError):
        tr.verificar("corto", "v1.a.b")


def test_el_motivo_no_filtra_el_token_ni_los_datos() -> None:
    token = _emitir()
    with pytest.raises(tr.TokenInvalido) as error:
        tr.verificar(SECRETO, token, correo="otra@ejemplo.co", ahora=AHORA)

    assert token not in str(error.value)
    assert "persona" not in str(error.value).lower()


def test_usa_la_hora_actual_por_defecto() -> None:
    token, expira_en = tr.emitir(SECRETO, REGISTRO, CLIENTE, CORREO)

    assert tr.verificar(SECRETO, token).expira_en == expira_en
