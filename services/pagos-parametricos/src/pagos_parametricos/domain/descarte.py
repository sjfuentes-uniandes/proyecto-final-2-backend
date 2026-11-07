"""Cola de descarte de `parametricos-eventos` (HU-W24, AC3; pantallas H10 y H11).

Un mensaje llega a la DLQ cuando agota sus recepciones (`maxReceiveCount` = 5, `catalog.tf`). Aquí
se interpreta: su causa (la que anotó el consumidor por `MessageId`), la póliza a la que se refiere
y la corrección que propone un analista. Funciones puras: sin SQS, base ni reloj.

- **Causa:** `FORMATO_INVALIDO` (sobre ilegible o que no valida contra su esquema),
  `REFERENCIA_INEXISTENTE` (el manejador no encontró la póliza o la cobertura),
  `ERROR_PROCESAMIENTO` (cualquier otro fallo) y `DESCONOCIDA` (sin causa anotada).
- **Corrección:** solo cambia campos que ya existen en el `payload` y por valores escalares; el
  `eventId` no cambia, así que la idempotencia del consumidor evita un segundo pago si ya se había
  pagado (H11: "El reproceso usa la misma clave de idempotencia del evento original").
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

# Desenlaces de `solventa_common.inbox` que no borran el mensaje.
ENVENENADO = "envenenado"
# Lo que lanza el manejador de HU-W21 cuando la póliza o la cobertura no existen. Por nombre de
# tipo: el consumidor anota `type(error).__name__`, nunca el mensaje.
ERRORES_REFERENCIA = frozenset({"PolizaInexistente", "CoberturaInexistente"})
# Dónde puede venir la póliza en el payload del evento observado (HU-W21 fija el esquema).
CAMPOS_POLIZA = ("numeroPoliza", "polizaNumero", "polizaId", "poliza")
LARGO_REFERENCIA = 64


class Causa(StrEnum):
    FORMATO_INVALIDO = "FORMATO_INVALIDO"
    REFERENCIA_INEXISTENTE = "REFERENCIA_INEXISTENTE"
    ERROR_PROCESAMIENTO = "ERROR_PROCESAMIENTO"
    DESCONOCIDA = "DESCONOCIDA"


class Accion(StrEnum):
    REPROCESADO = "REPROCESADO"
    CORREGIDO = "CORREGIDO"
    DESCARTADO = "DESCARTADO"


class CorreccionInvalida(Exception):
    """La corrección no se puede aplicar. `campos` son los que fallan (nunca sus valores)."""

    def __init__(self, motivo: str, campos: Sequence[str] = ()) -> None:
        super().__init__(motivo)
        self.motivo = motivo
        self.campos = list(campos)


@dataclass(frozen=True)
class FalloAnotado:
    """La última causa que anotó el consumidor para un `MessageId`."""

    outcome: str
    detalle: str | None
    recepciones: int | None


@dataclass(frozen=True)
class MensajeDescartado:
    message_id: str
    evento_id: str | None
    tipo: str | None
    cuerpo: str
    recibido_en: str | None
    intentos: int | None
    causa: Causa
    detalle: str | None
    poliza: str | None


def causa_de(fallo: FalloAnotado | None) -> Causa:
    if fallo is None:
        return Causa.DESCONOCIDA
    if fallo.outcome == ENVENENADO:
        return Causa.FORMATO_INVALIDO
    if fallo.detalle in ERRORES_REFERENCIA:
        return Causa.REFERENCIA_INEXISTENTE
    return Causa.ERROR_PROCESAMIENTO


def leer(cuerpo: str) -> dict[str, Any] | None:
    """El sobre como objeto (decimales como `Decimal`), o None si no es un objeto JSON."""
    try:
        sobre = json.loads(cuerpo, parse_float=Decimal)
    except ValueError:
        return None
    return sobre if isinstance(sobre, dict) else None


def poliza_de(sobre: Mapping[str, Any] | None) -> str | None:
    payload = sobre.get("payload") if sobre else None
    if not isinstance(payload, Mapping):
        return None
    for campo in CAMPOS_POLIZA:
        valor = payload.get(campo)
        if isinstance(valor, str) and 0 < len(valor) <= LARGO_REFERENCIA and valor.isprintable():
            return valor
    return None


def interpretar(
    message_id: str,
    cuerpo: str,
    *,
    recibido_en: str | None,
    recepciones: int | None,
    fallo: FalloAnotado | None,
) -> MensajeDescartado:
    sobre = leer(cuerpo)
    evento_id = sobre.get("eventId") if sobre else None
    tipo = sobre.get("type") if sobre else None
    return MensajeDescartado(
        message_id=message_id,
        evento_id=evento_id if isinstance(evento_id, str) else None,
        tipo=tipo if isinstance(tipo, str) else None,
        cuerpo=cuerpo,
        recibido_en=recibido_en,
        # Las recepciones que anotó el consumidor; si no, las que informa la DLQ.
        intentos=(fallo.recepciones if fallo and fallo.recepciones else recepciones),
        causa=causa_de(fallo),
        detalle=fallo.detalle if fallo else None,
        poliza=poliza_de(sobre),
    )


def resumen_por_causa(mensajes: Sequence[MensajeDescartado]) -> dict[Causa, int]:
    conteo: dict[Causa, int] = {}
    for mensaje in mensajes:
        conteo[mensaje.causa] = conteo.get(mensaje.causa, 0) + 1
    return conteo


def _escalar(valor: object) -> bool:
    return valor is None or isinstance(valor, str | int | float | bool | Decimal)


def aplicar_correccion(cuerpo: str, cambios: Mapping[str, object]) -> str:
    """El cuerpo con los campos del `payload` corregidos, el mismo `eventId` y lo demás intacto.

    Lanza `CorreccionInvalida` si el cuerpo no es un sobre, si no hay cambios, si un campo no existe
    en el `payload` o si un valor no es escalar.
    """
    # Sin Decimal: al reescribir, un número JSON vuelve con su valor (los montos de los contratos
    # son texto decimal y no pasan por aquí como número).
    try:
        sobre = json.loads(cuerpo)
    except ValueError:
        sobre = None
    if not isinstance(sobre, dict) or not isinstance(sobre.get("payload"), dict):
        raise CorreccionInvalida("el contenido no es un sobre con payload")
    if not cambios:
        raise CorreccionInvalida("la corrección no cambia nada")
    payload: dict[str, Any] = sobre["payload"]
    desconocidos = [campo for campo in cambios if campo not in payload]
    if desconocidos:
        raise CorreccionInvalida("campos que no están en el evento", desconocidos)
    no_escalares = [campo for campo, valor in cambios.items() if not _escalar(valor)]
    if no_escalares:
        raise CorreccionInvalida("solo se corrigen valores simples", no_escalares)
    corregido = {**sobre, "payload": {**payload, **cambios}}
    return json.dumps(corregido, ensure_ascii=False, separators=(",", ":"), default=str)
