"""El evento externo ya autenticado y validado, y su identidad (HU-W21, DS3-16)."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5


def evento_id(fuente: str, id_externo: str) -> UUID:
    """UUIDv5 determinista de `(fuente, idExterno)`: el mismo evento tiene siempre el mismo id, sin
    importar la marca de tiempo, el formato del cuerpo ni el día del envío. Es también el `eventId`
    del sobre, así el reproceso de HU-W24 (guía §7) reconstruye la misma identidad."""
    return uuid5(NAMESPACE_URL, f"telemetria/{fuente}/{id_externo}")


def clave_archivo(prefijo: str, recibido_en: datetime, evento: UUID) -> str:
    """`<prefijo><AAAA>/<MM>/<DD>/<eventoId>.json`, con la fecha UTC de la recepción."""
    utc = recibido_en.astimezone(UTC)
    return f"{prefijo}{utc:%Y}/{utc:%m}/{utc:%d}/{evento}.json"


@dataclass(frozen=True)
class EventoCanonico:
    """El modelo canónico interno: nada del formato de la fuente sale de aquí (§13, regla 6)."""

    fuente: str
    id_externo: str
    evento_origen: str
    tipo_evento: str
    valor_observado: str
    unidad: str
    zona: str
    observado_en: datetime

    @property
    def evento_id(self) -> UUID:
        return evento_id(self.fuente, self.id_externo)
