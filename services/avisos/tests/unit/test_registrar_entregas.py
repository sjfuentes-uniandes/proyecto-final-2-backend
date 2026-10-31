"""Caso de uso de registrar entregas (HU-M11): un aviso por dispositivo activo, sin duplicar por
evento. Usa un repositorio en memoria que reproduce la regla real de `crear_pendientes` (una entrega
PENDIENTE por dispositivo ACTIVO del cliente, sin tocar las ya existentes de (evento, dispositivo)),
sin depender de las pruebas de módulo contra Postgres.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from avisos.application.entregas.registrar import RegistrarEntregas
from avisos.domain.avisos.modelos import Aviso

pytestmark = pytest.mark.anyio

AHORA = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


class Reloj:
    def ahora(self) -> datetime:
        return AHORA


@dataclass(frozen=True, slots=True)
class _Dispositivo:
    id: UUID
    cliente_id: UUID
    activo: bool = True


class RepoEntregasMemoria:
    """Reproduce la regla de `crear_pendientes`: una entrega PENDIENTE por dispositivo ACTIVO del
    cliente; (evento, dispositivo) ya existente no se toca de nuevo (como el ON CONFLICT real)."""

    def __init__(self, dispositivos: list[_Dispositivo]) -> None:
        self._dispositivos = dispositivos
        self.claves: set[tuple[UUID, UUID]] = set()

    async def crear_pendientes(
        self, aviso: Aviso, cliente_id: UUID, ahora: datetime, tx: object
    ) -> int:
        creadas = 0
        for dispositivo in self._dispositivos:
            if dispositivo.cliente_id != cliente_id or not dispositivo.activo:
                continue
            clave = (aviso.event_id, dispositivo.id)
            if clave in self.claves:
                continue
            self.claves.add(clave)
            creadas += 1
        return creadas


async def test_dos_dispositivos_validos_reciben_un_aviso_cada_uno_y_sin_duplicar() -> None:
    cliente_id = uuid4()
    d1, d2 = _Dispositivo(uuid4(), cliente_id), _Dispositivo(uuid4(), cliente_id)
    repo = RepoEntregasMemoria([d1, d2])
    registrar = RegistrarEntregas(repo, Reloj())
    event_id, poliza_id = uuid4(), uuid4()

    creadas = await registrar.poliza_emitida(event_id, poliza_id, cliente_id, tx=object())

    assert creadas == 2
    assert repo.claves == {(event_id, d1.id), (event_id, d2.id)}

    # El mismo evento no debe volver a crear entregas para los dispositivos que ya la tienen.
    repetidas = await registrar.poliza_emitida(event_id, poliza_id, cliente_id, tx=object())

    assert repetidas == 0
    assert repo.claves == {(event_id, d1.id), (event_id, d2.id)}
