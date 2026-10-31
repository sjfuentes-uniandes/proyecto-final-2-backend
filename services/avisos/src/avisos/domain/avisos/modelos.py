"""Dominio de avisos (HU-M11): dispositivos, entregas y el aviso de una póliza emitida.

El aviso es un mensaje solo de datos: `tipo`, `polizaId` y `eventId`. Sin número, producto, montos ni
nombre; el texto localizado lo arma la app, así que nada sensible llega a la pantalla bloqueada.

Garantía de entrega: a lo sumo un aviso visible por (evento, dispositivo). La clave única de
`entrega` y el inbox evitan los duplicados de SQS; si el proceso cae después de que el proveedor
aceptó el push y antes de guardar ENVIADO, la entrega se reclama de nuevo y puede salir un segundo
push. Por eso cada push lleva `eventId` como clave de colapso (la app lo usa como etiqueta de la
notificación) y el segundo reemplaza al primero en lugar de mostrarse aparte.
"""

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

TIPO_POLIZA_EMITIDA = "POLIZA_EMITIDA"
CODIGO = re.compile(r"[A-Z0-9_]{1,40}")


class EstadoDispositivo(StrEnum):
    ACTIVO = "ACTIVO"
    INVALIDO = "INVALIDO"


class EstadoEntrega(StrEnum):
    PENDIENTE = "PENDIENTE"
    EN_ENVIO = "EN_ENVIO"
    ENVIADO = "ENVIADO"
    TOKEN_INVALIDO = "TOKEN_INVALIDO"
    FALLIDO = "FALLIDO"


MOTIVO_TOKEN_RECHAZADO = "TOKEN_RECHAZADO"
MOTIVO_DISPOSITIVO_INVALIDO = "DISPOSITIVO_INVALIDO"
FALLO_REINTENTOS_AGOTADOS = "REINTENTOS_AGOTADOS"
FALLO_LEASE_VENCIDO = "LEASE_VENCIDO"
FALLO_INESPERADO = "ERROR_INESPERADO"


def hash_token(token: str) -> str:
    """Identidad del token para la unicidad: sha256 en hexadecimal. El token no se registra."""
    return hashlib.sha256(token.encode()).hexdigest()


def es_codigo(valor: str) -> bool:
    """Los códigos de fallo y de motivo son constantes, nunca texto libre."""
    return CODIGO.fullmatch(valor) is not None


@dataclass(frozen=True, slots=True)
class Aviso:
    """Contenido del push: solo datos."""

    tipo: str
    poliza_id: UUID
    event_id: UUID

    @property
    def clave_colapso(self) -> str:
        return str(self.event_id)


def aviso_de_poliza_emitida(event_id: UUID, poliza_id: UUID) -> Aviso:
    return Aviso(tipo=TIPO_POLIZA_EMITIDA, poliza_id=poliza_id, event_id=event_id)


@dataclass(frozen=True, slots=True)
class Dispositivo:
    id: UUID
    cliente_id: UUID
    token_hash: str
    estado: EstadoDispositivo = EstadoDispositivo.ACTIVO
    invalidado_en: datetime | None = None
    motivo_invalidez: str | None = None

    def invalidar(self, ahora: datetime, motivo: str) -> "Dispositivo":
        """Terminal: un dispositivo INVALIDO solo vuelve a ACTIVO con un nuevo registro del token."""
        if self.estado is EstadoDispositivo.INVALIDO:
            return self
        if not es_codigo(motivo):
            raise ValueError("el motivo debe ser un código")
        return replace(
            self, estado=EstadoDispositivo.INVALIDO, invalidado_en=ahora, motivo_invalidez=motivo
        )


@dataclass(frozen=True, slots=True)
class Entrega:
    """Una entrega reclamada para enviar. `token` es secreto: el `repr` no lo muestra."""

    id: UUID
    dispositivo_id: UUID
    aviso: Aviso
    intentos: int
    dispositivo_activo: bool
    token: str

    def __repr__(self) -> str:
        return f"Entrega(id={self.id}, intentos={self.intentos})"


@dataclass(frozen=True, slots=True)
class PoliticaReintento:
    """Espera creciente: `espera_inicial_s`, duplicando hasta `espera_max_s`. `max_intentos` incluye
    el primero."""

    max_intentos: int = 5
    espera_inicial_s: float = 5.0
    espera_max_s: float = 300.0

    def proximo_intento(self, intentos: int, ahora: datetime) -> datetime | None:
        """Cuándo reintentar tras el intento número `intentos`; None si ya se agotaron."""
        if intentos >= self.max_intentos:
            return None
        espera = min(self.espera_inicial_s * 2 ** max(intentos - 1, 0), self.espera_max_s)
        return ahora + timedelta(seconds=espera)
