"""Pruebas del Lambda pre sign-up: sin AWS, con el secreto de Secrets Manager simulado."""

import importlib.util
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

RAIZ = Path(__file__).resolve().parents[3]
LIBRERIA = RAIZ / "libs" / "solventa-common" / "src" / "solventa_common"
SECRETO = "secreto-sintetico-de-prueba-0123456789"
CLIENTE_MOVIL = "cliente-movil-sintetico"
CLIENTE_WEB = "cliente-web-sintetico"
CORREO = "persona@ejemplo.co"
ARN = "arn:aws:secretsmanager:us-east-1:1:secret:s"


@pytest.fixture
def handler(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    """Carga el handler como lo ve Lambda: `token_registro.py` junto a él, en la raíz."""
    monkeypatch.syspath_prepend(str(LIBRERIA))
    monkeypatch.setenv("REGISTRO_TOKEN_SECRET_ARN", ARN)
    especificacion = importlib.util.spec_from_file_location(
        "pre_sign_up_handler", Path(__file__).parent / "handler.py"
    )
    assert especificacion is not None and especificacion.loader is not None
    modulo = importlib.util.module_from_spec(especificacion)
    sys.modules["pre_sign_up_handler"] = modulo
    especificacion.loader.exec_module(modulo)
    yield modulo
    sys.modules.pop("pre_sign_up_handler", None)
    sys.modules.pop("token_registro", None)


class SecretsManagerFalso:
    def __init__(self, cuerpo: dict[str, str] | Exception) -> None:
        self.cuerpo = cuerpo
        self.llamadas: list[str] = []

    def get_secret_value(self, SecretId: str) -> dict[str, str]:
        self.llamadas.append(SecretId)
        if isinstance(self.cuerpo, Exception):
            raise self.cuerpo
        return {"SecretString": json.dumps(self.cuerpo)}


def _usar_secretos(monkeypatch: pytest.MonkeyPatch, cuerpo: Any) -> SecretsManagerFalso:
    falso = SecretsManagerFalso(cuerpo)
    boto3 = ModuleType("boto3")
    boto3.client = lambda servicio: falso  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    return falso


@pytest.fixture
def secretos(monkeypatch: pytest.MonkeyPatch) -> SecretsManagerFalso:
    return _usar_secretos(monkeypatch, {"secret": SECRETO, "mobile_client_id": CLIENTE_MOVIL})


def _token(handler: ModuleType, correo: str = CORREO, ttl_s: int = 1800) -> str:
    token, _ = handler.token_registro.emitir(
        SECRETO, "registro-1", "cliente-1", correo, ttl_s=ttl_s
    )
    return token


def _evento(
    token: str | None,
    correo: str = CORREO,
    cliente: str = CLIENTE_MOVIL,
    origen: str = "PreSignUp_SignUp",
) -> dict[str, Any]:
    metadata = None if token is None else {"tokenRegistro": token}
    return {
        "triggerSource": origen,
        "callerContext": {"clientId": cliente},
        "request": {"userAttributes": {"email": correo}, "clientMetadata": metadata},
        "response": {"autoConfirmUser": False, "autoVerifyEmail": False},
    }


def test_con_un_token_valido_deja_crear_la_cuenta_sin_confirmarla(
    handler: ModuleType, secretos: SecretsManagerFalso
) -> None:
    evento = _evento(_token(handler))

    resultado = handler.handler(evento, None)

    assert resultado is evento
    assert resultado["response"] == {"autoConfirmUser": False, "autoVerifyEmail": False}


def test_el_correo_se_compara_sin_importar_mayusculas(
    handler: ModuleType, secretos: SecretsManagerFalso
) -> None:
    assert handler.handler(_evento(_token(handler), correo="PERSONA@Ejemplo.co"), None)


@pytest.mark.parametrize(
    ("armar", "motivo"),
    [
        (lambda h: _evento(None), "FORMATO"),
        (lambda h: _evento(""), "FORMATO"),
        (lambda h: _evento("basura"), "FORMATO"),
        (lambda h: _evento(_token(h)[:-3] + "AAA"), "FIRMA"),
        (lambda h: _evento(_token(h), correo="otra@ejemplo.co"), "CORREO"),
        (lambda h: _evento(_token(h, ttl_s=-5)), "VENCIDO"),
        (lambda h: _evento(_token(h), correo=""), "CORREO"),
    ],
)
def test_sin_token_valido_rechaza_el_registro_de_la_app(
    handler: ModuleType,
    secretos: SecretsManagerFalso,
    armar: Any,
    motivo: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")

    with pytest.raises(handler.RegistroNoAutorizado, match=handler.REGISTRO_NO_AUTORIZADO):
        handler.handler(armar(handler), None)

    assert motivo in caplog.text


def test_sin_clientmetadata_rechaza(handler: ModuleType, secretos: SecretsManagerFalso) -> None:
    evento = _evento(None)
    evento["request"].pop("clientMetadata")

    with pytest.raises(handler.RegistroNoAutorizado):
        handler.handler(evento, None)


def test_un_token_firmado_con_otro_secreto_rechaza(
    handler: ModuleType, secretos: SecretsManagerFalso
) -> None:
    ajeno, _ = handler.token_registro.emitir(
        "otro-secreto-sintetico-de-prueba-0123456789", "r", "c", CORREO
    )

    with pytest.raises(handler.RegistroNoAutorizado):
        handler.handler(_evento(ajeno), None)


def test_el_portal_web_no_exige_el_token(
    handler: ModuleType, secretos: SecretsManagerFalso
) -> None:
    evento = _evento(None, cliente=CLIENTE_WEB)

    assert handler.handler(evento, None) is evento


@pytest.mark.parametrize("origen", ["PreSignUp_AdminCreateUser", "PreSignUp_ExternalProvider"])
def test_otros_origenes_no_aplican_y_no_consultan_secretos(
    handler: ModuleType, secretos: SecretsManagerFalso, origen: str
) -> None:
    evento = _evento(None, origen=origen)

    assert handler.handler(evento, None) is evento
    assert secretos.llamadas == []


def test_lee_el_secreto_una_sola_vez_por_instancia(
    handler: ModuleType, secretos: SecretsManagerFalso
) -> None:
    token = _token(handler)

    handler.handler(_evento(token), None)
    handler.handler(_evento(token), None)

    assert secretos.llamadas == [ARN]


@pytest.mark.parametrize(
    "cuerpo",
    [
        RuntimeError("Secrets Manager caído"),
        {"secret": SECRETO},
        {"mobile_client_id": CLIENTE_MOVIL},
    ],
)
def test_falla_cerrado_si_no_puede_leer_la_configuracion(
    handler: ModuleType, monkeypatch: pytest.MonkeyPatch, cuerpo: Any
) -> None:
    _usar_secretos(monkeypatch, cuerpo)

    with pytest.raises((RuntimeError, KeyError)):
        handler.handler(_evento(_token(handler)), None)


def test_los_logs_no_contienen_el_correo_ni_el_token(
    handler: ModuleType, secretos: SecretsManagerFalso, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO")
    token = _token(handler, correo="centinela-734519@ejemplo.co")

    handler.handler(_evento(token, correo="centinela-734519@ejemplo.co"), None)
    with pytest.raises(handler.RegistroNoAutorizado):
        handler.handler(_evento(token, correo="otra-734519@ejemplo.co"), None)

    assert "registro autorizado" in caplog.text and "CORREO" in caplog.text
    assert "734519" not in caplog.text and token not in caplog.text and SECRETO not in caplog.text


def test_un_evento_con_forma_inesperada_no_se_deja_pasar(
    handler: ModuleType, secretos: SecretsManagerFalso
) -> None:
    with pytest.raises(KeyError):
        handler.handler({"triggerSource": "PreSignUp_SignUp", "request": {}}, None)
