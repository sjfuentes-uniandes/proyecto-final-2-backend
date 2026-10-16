"""Lambda pre sign-up del pool `clientes` de Cognito (HU-M06).

Rechaza el registro desde la app móvil si no trae un token de registro válido: es decir, si la
persona no pasó antes la verificación biométrica. El token lo emite `clientes` y va ligado al
correo con el que se crea la cuenta; aquí solo se valida su firma y vigencia, sin llamar a ningún
servicio (por eso el Lambda no necesita estar en la VPC).

Se empaqueta con `token_registro.py` de libs/solventa-common (infra/platform/registro.tf).
Solo usa la biblioteca estándar y boto3, que ya trae el runtime.
"""

import json
import logging
import os
from typing import Any

import token_registro

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Cognito muestra este texto a la app como UserLambdaValidationException.
REGISTRO_NO_AUTORIZADO = "REGISTRO_NO_AUTORIZADO"


class RegistroNoAutorizado(Exception):
    """El registro no trae un token de registro válido: Cognito lo rechaza."""


_configuracion: dict[str, str] | None = None


def _cargar_configuracion() -> dict[str, str]:
    """{secret, mobile_client_id} del secreto de Secrets Manager; se lee una vez por instancia."""
    global _configuracion
    if _configuracion is None:
        import boto3

        respuesta = boto3.client("secretsmanager").get_secret_value(
            SecretId=os.environ["REGISTRO_TOKEN_SECRET_ARN"]
        )
        _configuracion = json.loads(respuesta["SecretString"])
    return _configuracion


def _registrar(mensaje: str, **datos: str) -> None:
    # Nunca el correo, el token ni el secreto: solo categorías.
    logger.info(json.dumps({"mensaje": mensaje, **datos}))


def handler(event: dict[str, Any], _contexto: object) -> dict[str, Any]:
    """Falla cerrado: si no puede validar, el registro se rechaza."""
    if event.get("triggerSource") != "PreSignUp_SignUp":
        return event  # alta por un administrador o proveedor externo: no aplica
    configuracion = _cargar_configuracion()
    if event["callerContext"]["clientId"] != configuracion["mobile_client_id"]:
        return event  # el portal web no usa el registro biométrico de la app
    solicitud = event["request"]
    token = (solicitud.get("clientMetadata") or {}).get("tokenRegistro", "")
    correo = solicitud.get("userAttributes", {}).get("email", "")
    try:
        token_registro.verificar(configuracion["secret"], token, correo=correo)
    except token_registro.TokenInvalido as error:
        _registrar("registro rechazado", motivo=error.motivo)
        raise RegistroNoAutorizado(REGISTRO_NO_AUTORIZADO) from None
    _registrar("registro autorizado")
    return event
