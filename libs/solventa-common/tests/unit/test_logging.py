import io
import json
import logging
from collections.abc import Callable

import pytest
from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator
from opentelemetry.sdk.trace import TracerProvider
from solventa_common import correlation
from solventa_common.logging import (
    MAX_TEXTO,
    REDACTADO,
    configurar_logging,
    enmascarar,
    es_clave_sensible,
    sanitizar,
    trace_id_xray,
)

SECRETO = "s3cr3t0-que-no-debe-salir"
TOKEN = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjMifQ.firma-del-token"


@pytest.mark.parametrize(
    "clave",
    [
        "password", "Password", "db_password", "secret", "client_secret", "CLIENT-SECRET", "token",
        "access_token", "refreshToken", "authorization", "Authorization", "api_key", "x-api-key", "apiKey",
        "selfie", "selfie_base64", "biometria", "datos_biometricos", "imagen", "imagen_documento",
        "cuenta", "numero_cuenta", "ingresos", "ingresos_mensuales", "obligaciones", "obligaciones_financieras",
        "cookie", "set-cookie", "credenciales", "pin", "cvv", "contraseña",
    ],
)
def test_claves_sensibles(clave: str) -> None:
    assert es_clave_sensible(clave)


@pytest.mark.parametrize("clave", ["operation", "service", "status", "correlation_id", "socio_id", "producto", "opinion"])
def test_claves_normales(clave: str) -> None:
    assert not es_clave_sensible(clave)


def test_sanitiza_casos_anidados() -> None:
    entrada = {
        "socio": "SOC-000114",
        "integracion": {"client_id": "abc", "client_secret": SECRETO},
        "solicitud": {
            "headers": {"Authorization": f"Bearer {TOKEN}", "X-Correlation-Id": "prueba-123"},
            "cliente": {
                "documento": "CC-1",
                "perfil_financiero": {"ingresos": 9_500_000, "obligaciones": [{"entidad": "B", "saldo": 1}]},
                "kyc": [{"selfie": "iVBORw0KGgo" * 100}, {"biometria": {"plantilla": "x"}}],
            },
        },
        "listas": [[{"password": SECRETO}]],
    }
    salida = sanitizar(entrada)
    assert salida["socio"] == "SOC-000114"
    assert salida["integracion"] == {"client_id": "abc", "client_secret": REDACTADO}
    assert salida["solicitud"]["headers"]["Authorization"] == REDACTADO
    assert salida["solicitud"]["headers"]["X-Correlation-Id"] == "prueba-123"
    financiero = salida["solicitud"]["cliente"]["perfil_financiero"]
    assert financiero == {"ingresos": REDACTADO, "obligaciones": REDACTADO}
    assert salida["solicitud"]["cliente"]["kyc"] == [{"selfie": REDACTADO}, {"biometria": REDACTADO}]
    assert salida["listas"] == [[{"password": REDACTADO}]]
    texto = json.dumps(salida)
    assert SECRETO not in texto and TOKEN not in texto and "9500000" not in texto
    # La entrada no se modifica.
    assert entrada["integracion"]["client_secret"] == SECRETO


def test_trunca_cuerpos_largos_y_binarios() -> None:
    cuerpo = "a" * (MAX_TEXTO * 4)
    salida = sanitizar({"cuerpo": cuerpo, "archivo": b"\x89PNG" * 1000, "lista": list(range(500))})
    assert len(salida["cuerpo"]) < MAX_TEXTO + 50
    assert salida["cuerpo"].endswith(f"[truncado {MAX_TEXTO * 3} caracteres]")
    assert salida["archivo"] == "[binario 4000 bytes]"
    assert len(salida["lista"]) == 51 and salida["lista"][-1] == "[450 elementos omitidos]"


def test_limita_la_profundidad() -> None:
    anidado: dict = {}
    actual = anidado
    for _ in range(20):
        actual["n"] = {}
        actual = actual["n"]
    assert "[profundidad máxima]" in json.dumps(sanitizar(anidado), ensure_ascii=False)


def test_enmascara_texto_libre() -> None:
    texto = enmascarar(f"llamada con Authorization: Bearer {TOKEN} password={SECRETO} api_key: {SECRETO} token={TOKEN}")
    assert SECRETO not in texto and TOKEN not in texto
    assert enmascarar(f"Bearer {TOKEN}") == "Bearer [REDACTADO]"


def test_modelos_pydantic() -> None:
    from pydantic import BaseModel

    class Solicitud(BaseModel):
        usuario: str
        password: str

    assert sanitizar(Solicitud(usuario="u", password=SECRETO)) == {"usuario": "u", "password": REDACTADO}


def test_log_json_con_campos_del_contrato(capturar_logs: Callable[..., io.StringIO]) -> None:
    salida = capturar_logs("api-socios", "int")
    token = correlation.establecer("prueba-123")
    try:
        logging.getLogger("prueba").info(
            "socio creado",
            extra={"operation": "crear_socio", "result": "ok", "duration_ms": 12.5, "client_secret": SECRETO},
        )
    finally:
        correlation.restablecer(token)
    linea = json.loads(salida.getvalue())
    assert {k: linea[k] for k in ("level", "service", "environment", "operation", "result", "duration_ms", "correlation_id")} == {
        "level": "INFO", "service": "api-socios", "environment": "int", "operation": "crear_socio",
        "result": "ok", "duration_ms": 12.5, "correlation_id": "prueba-123",
    }
    assert linea["timestamp"].endswith("+00:00")
    assert linea["client_secret"] == REDACTADO
    assert SECRETO not in salida.getvalue()


def test_log_incluye_trace_id_en_formato_xray(capturar_logs: Callable[..., io.StringIO]) -> None:
    salida = capturar_logs()
    tracer = TracerProvider(id_generator=AwsXRayIdGenerator()).get_tracer("prueba")
    with tracer.start_as_current_span("x") as span:
        logging.getLogger("prueba").info("dentro de un span")
        esperado = trace_id_xray(span.get_span_context().trace_id)
    linea = json.loads(salida.getvalue())
    assert linea["trace_id"] == esperado
    assert linea["trace_id"].startswith("1-") and len(linea["trace_id"]) == 35


def test_excepciones_sin_secretos(capturar_logs: Callable[..., io.StringIO]) -> None:
    salida = capturar_logs()
    try:
        raise ValueError(f"fallo con password={SECRETO}")
    except ValueError:
        logging.getLogger("prueba").exception("error inesperado")
    linea = json.loads(salida.getvalue())
    assert linea["error_type"] == "ValueError"
    assert SECRETO not in salida.getvalue()


def test_configurar_logging_reemplaza_handlers_y_silencia_access_log(capsys: pytest.CaptureFixture[str]) -> None:
    raiz = logging.getLogger()
    anteriores = list(raiz.handlers)
    try:
        configurar_logging("catalogo", "int")
        logging.getLogger("uvicorn.error").info("arranque")
        logging.getLogger("uvicorn.access").info('GET /x?token=%s 200', SECRETO)
        lineas = [json.loads(linea) for linea in capsys.readouterr().out.splitlines()]
        assert [linea["message"] for linea in lineas] == ["arranque"]
        assert lineas[0]["service"] == "catalogo"
    finally:
        raiz.handlers = anteriores
        logging.getLogger("uvicorn.access").disabled = False
