"""Token de registro firmado (HU-M06): prueba que una verificación biométrica fue exitosa.

Lo emite `clientes` al verificar la identidad y lo valida el Lambda pre sign-up de Cognito y el
vínculo del primer ingreso. Solo usa la biblioteca estándar: el Lambda lo empaqueta sin
dependencias. Formato: `v1.<payload en base64url>.<firma HMAC-SHA256 en base64url>`.

El token no se guarda ni se consulta a un servicio: quien lo valida solo necesita el secreto.
Va ligado al registro, al cliente y al correo, así que no sirve para crear una cuenta con otro
correo.
"""

import base64
import binascii
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

VERSION = "v1"
TTL_S = 30 * 60
SECRETO_MIN = 32
# Solo para el ambiente local; en AWS el secreto viene de Secrets Manager y es obligatorio.
SECRETO_LOCAL = "solo-para-local-no-usar-en-aws-0123456789"


class TokenInvalido(Exception):
    """El token no se puede usar. `motivo` nunca incluye el token ni sus datos."""

    def __init__(self, motivo: str) -> None:
        super().__init__(motivo)
        self.motivo = motivo


@dataclass(frozen=True, slots=True)
class ClaimsRegistro:
    registro_id: str
    cliente_id: str
    correo: str
    expira_en: int


def normalizar_correo(correo: str) -> str:
    return correo.strip().lower()


def _b64(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode("ascii")


def _desde_b64(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


def _firma(secreto: str, mensaje: str) -> str:
    if len(secreto) < SECRETO_MIN:
        raise ValueError(f"El secreto del token debe tener al menos {SECRETO_MIN} caracteres")
    return _b64(hmac.new(secreto.encode(), mensaje.encode("ascii"), hashlib.sha256).digest())


def emitir(
    secreto: str,
    registro_id: str,
    cliente_id: str,
    correo: str,
    *,
    ahora: int | None = None,
    ttl_s: int = TTL_S,
) -> tuple[str, int]:
    """Devuelve el token y su vencimiento (segundos desde la época)."""
    expira_en = (int(time.time()) if ahora is None else ahora) + ttl_s
    carga = {
        "rid": registro_id,
        "cid": cliente_id,
        "eml": normalizar_correo(correo),
        "exp": expira_en,
    }
    payload = _b64(json.dumps(carga, separators=(",", ":"), sort_keys=True).encode())
    mensaje = f"{VERSION}.{payload}"
    return f"{mensaje}.{_firma(secreto, mensaje)}", expira_en


def verificar(
    secreto: str,
    token: str,
    *,
    correo: str | None = None,
    registro_id: str | None = None,
    ahora: int | None = None,
    gracia_s: int = 0,
) -> ClaimsRegistro:
    """Valida firma, vigencia y, si se piden, el correo y el registro. Lanza TokenInvalido.

    `gracia_s` acepta un token vencido hasta esos segundos: el vínculo del primer ingreso ocurre
    después de confirmar el correo, que puede tardar más que la ventana del registro.
    """
    partes = token.split(".") if isinstance(token, str) else []
    if len(partes) != 3 or partes[0] != VERSION:
        raise TokenInvalido("FORMATO")
    mensaje = f"{partes[0]}.{partes[1]}"
    if not hmac.compare_digest(_firma(secreto, mensaje), partes[2]):
        raise TokenInvalido("FIRMA")
    try:
        carga = json.loads(_desde_b64(partes[1]))
        claims = ClaimsRegistro(carga["rid"], carga["cid"], carga["eml"], carga["exp"])
    except (ValueError, KeyError, TypeError, binascii.Error) as error:
        raise TokenInvalido("FORMATO") from error
    if not all(isinstance(v, str) for v in (claims.registro_id, claims.cliente_id, claims.correo)):
        raise TokenInvalido("FORMATO")
    if isinstance(claims.expira_en, bool) or not isinstance(claims.expira_en, int):
        raise TokenInvalido("FORMATO")
    if claims.expira_en + gracia_s <= (int(time.time()) if ahora is None else ahora):
        raise TokenInvalido("VENCIDO")
    if registro_id is not None and claims.registro_id != registro_id:
        raise TokenInvalido("REGISTRO")
    if correo is not None and claims.correo != normalizar_correo(correo):
        raise TokenInvalido("CORREO")
    return claims
