"""Casos de uso RegistrarDispositivo y DarDeBajaDispositivo: delegan en el repositorio y el reloj."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from avisos.application.dispositivos.casos import DarDeBajaDispositivo, RegistrarDispositivo

pytestmark = pytest.mark.anyio
AHORA = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


class Reloj:
    def ahora(self) -> datetime:
        return AHORA


class RepoFalso:
    def __init__(self, dispositivo_id: UUID | None = None) -> None:
        self.dispositivo_id = dispositivo_id or uuid4()
        self.registros: list[tuple[UUID, str, datetime]] = []
        self.bajas: list[tuple[UUID, UUID, datetime]] = []

    async def registrar(self, cliente_id: UUID, token: str, ahora: datetime) -> UUID:
        self.registros.append((cliente_id, token, ahora))
        return self.dispositivo_id

    async def dar_de_baja(self, dispositivo_id: UUID, cliente_id: UUID, ahora: datetime) -> None:
        self.bajas.append((dispositivo_id, cliente_id, ahora))


async def test_registrar_dispositivo_delega_con_la_hora_del_reloj() -> None:
    repo = RepoFalso()
    cliente_id = uuid4()
    caso = RegistrarDispositivo(repo, Reloj())  # type: ignore[arg-type]

    resultado = await caso.ejecutar(cliente_id, "token-sintetico-entregado")

    assert resultado == repo.dispositivo_id
    assert repo.registros == [(cliente_id, "token-sintetico-entregado", AHORA)]


async def test_dar_de_baja_dispositivo_delega_con_la_hora_del_reloj() -> None:
    repo = RepoFalso()
    cliente_id, dispositivo_id = uuid4(), uuid4()
    caso = DarDeBajaDispositivo(repo, Reloj())  # type: ignore[arg-type]

    await caso.ejecutar(cliente_id, dispositivo_id)

    assert repo.bajas == [(dispositivo_id, cliente_id, AHORA)]
