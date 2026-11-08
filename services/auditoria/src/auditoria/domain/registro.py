"""Registro de auditoría encadenado (HU-W24, D3).

`RegistroAuditoria` es el contrato de `diagrama_flujo_estructuras_informacion.puml`: actor + acción,
`resourceId`, `timestamp` y `previousHash`. Cada registro guarda además el evento tal como llegó
(`evento`, el texto del sobre), su `eventId` y su `correlationId`, y su propio `hash`:

    hash = SHA-256 del JSON canónico del registro sin `hash` (claves ordenadas, sin espacios)

`previousHash` es el `hash` del registro anterior de la cadena (el primero de todos usa `GENESIS`).
Alterar, quitar o reordenar un registro rompe la cadena desde ese punto (`verificar_cadena`).

Un **lote** es lo que se escribe en un objeto: los registros nuevos de un lote recibido de SQS, en
orden determinista (`occurredAt`, `eventId`), con su `secuencia` (consecutiva) y el `previousHash`
del primero. Funciones puras: sin reloj ni E/S.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

GENESIS = "0" * 64
ACTOR_SISTEMA = "sistema"
# Un actor es un identificador (asesor-01, sub de Cognito, servicio); otra cosa no se registra.
_LARGO_ACTOR = 128


class CadenaRota(Exception):
    """La cadena no verifica. Lleva la secuencia del primer lote que falla."""

    def __init__(self, secuencia: int, motivo: str) -> None:
        super().__init__(f"lote {secuencia}: {motivo}")
        self.secuencia = secuencia
        self.motivo = motivo


@dataclass(frozen=True)
class EventoAuditable:
    """Lo mínimo de un sobre para auditarlo, más el texto tal como llegó."""

    event_id: str
    event_type: str
    aggregate_id: str
    occurred_at: str
    correlation_id: str | None
    actor: str
    cuerpo: str


def actor_de(payload: object) -> str:
    """`payload.actorId` si es un identificador; si no, `sistema` (los eventos sin actor humano)."""
    if isinstance(payload, Mapping):
        actor = payload.get("actorId")
        if isinstance(actor, str) and 0 < len(actor) <= _LARGO_ACTOR and actor.isprintable():
            return actor
    return ACTOR_SISTEMA


def _canonico(datos: Mapping[str, Any]) -> bytes:
    return json.dumps(datos, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _huella(datos: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonico(datos)).hexdigest()


@dataclass(frozen=True)
class Lote:
    secuencia: int
    previous_hash: str
    registros: tuple[Mapping[str, Any], ...]

    @property
    def hash(self) -> str:
        """El `hash` del último registro: la nueva cabeza de la cadena."""
        return str(self.registros[-1]["hash"]) if self.registros else self.previous_hash

    def documento(self) -> dict[str, Any]:
        return {
            "secuencia": self.secuencia,
            "previousHash": self.previous_hash,
            "hash": self.hash,
            "registros": list(self.registros),
        }

    def contenido(self) -> bytes:
        return _canonico(self.documento())


def sellar(
    secuencia: int, previous_hash: str, eventos: Sequence[EventoAuditable], registrado_en: str
) -> Lote:
    """Encadena los eventos en orden (`occurredAt`, `eventId`) a partir de `previous_hash`."""
    if not eventos:
        raise ValueError("un lote necesita al menos un evento")
    registros: list[Mapping[str, Any]] = []
    anterior = previous_hash
    for evento in sorted(eventos, key=lambda e: (e.occurred_at, e.event_id)):
        registro: dict[str, Any] = {
            "actor": evento.actor,
            "accion": evento.event_type,
            "resourceId": evento.aggregate_id,
            "timestamp": evento.occurred_at,
            "eventId": evento.event_id,
            "correlationId": evento.correlation_id,
            "registradoEn": registrado_en,
            "evento": evento.cuerpo,
            "previousHash": anterior,
        }
        registro["hash"] = _huella(registro)
        anterior = registro["hash"]
        registros.append(registro)
    return Lote(secuencia, previous_hash, tuple(registros))


def verificar_cadena(lotes: Sequence[Mapping[str, Any]], desde: str = GENESIS) -> str:
    """Comprueba los documentos de lote en orden de `secuencia`; devuelve el `hash` final.

    Lanza `CadenaRota` en el primer lote con una secuencia que no sigue, un `previousHash` que no
    enlaza o un registro alterado.
    """
    anterior = desde
    esperada: int | None = None
    for lote in lotes:
        secuencia = int(lote["secuencia"])
        if esperada is not None and secuencia != esperada:
            raise CadenaRota(secuencia, "secuencia fuera de orden")
        if lote["previousHash"] != anterior:
            raise CadenaRota(secuencia, "previousHash no enlaza")
        for registro in lote["registros"]:
            sin_hash = {k: v for k, v in registro.items() if k != "hash"}
            if registro["previousHash"] != anterior or _huella(sin_hash) != registro["hash"]:
                raise CadenaRota(secuencia, "registro alterado")
            anterior = registro["hash"]
        if lote["hash"] != anterior:
            raise CadenaRota(secuencia, "hash del lote no coincide")
        esperada = secuencia + 1
    return anterior
