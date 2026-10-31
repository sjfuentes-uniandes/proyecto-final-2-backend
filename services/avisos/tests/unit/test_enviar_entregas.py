"""Casos de uso de envío de entregas, con un repositorio en memoria."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from avisos.application.entregas.barrido import Barrido
from avisos.application.entregas.enviar import LOTE_MAXIMO, EnviarEntregas
from avisos.application.entregas.puertos import FalloTransitorio
from avisos.domain.avisos.modelos import (
    Aviso,
    Entrega,
    EstadoEntrega,
    PoliticaReintento,
    aviso_de_poliza_emitida,
)

pytestmark = pytest.mark.anyio
AHORA = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


class PushEnMemoria:
    """EnviadorPush en memoria (HU-M11): doble de prueba de los casos de uso de envío.

    Registra lo enviado y permite simular el rechazo y los fallos transitorios por token."""

    def __init__(self) -> None:
        self.enviados: list[tuple[str, Aviso]] = []
        self.intentos: list[str] = []
        self.rechazados: set[str] = set()
        # Fallos transitorios pendientes por token: cada intento consume uno.
        self.fallos: dict[str, list[str]] = {}

    async def enviar(self, token: str, aviso: Aviso) -> bool:
        self.intentos.append(token)
        if self.fallos.get(token):
            raise FalloTransitorio(self.fallos[token].pop(0))
        if token in self.rechazados:
            return False
        self.enviados.append((token, aviso))
        return True


class Reloj:
    def __init__(self) -> None:
        self.t = AHORA

    def ahora(self) -> datetime:
        return self.t


class _Fila:
    def __init__(self, aviso: Aviso, token: str, activo: bool = True) -> None:
        self.id = uuid4()
        self.dispositivo_id = uuid4()
        self.aviso = aviso
        self.token = token
        self.activo = activo
        self.estado = EstadoEntrega.PENDIENTE
        self.intentos = 0
        self.proximo = AHORA
        self.hasta: datetime | None = None
        self.fallo: str | None = None


class RepoMemoria:
    def __init__(self) -> None:
        self.filas: list[_Fila] = []

    def agregar(self, aviso: Aviso, token: str, activo: bool = True) -> _Fila:
        fila = _Fila(aviso, token, activo)
        self.filas.append(fila)
        return fila

    async def crear_pendientes(self, *args: Any) -> int:
        raise NotImplementedError

    async def reclamar(
        self, ahora: datetime, hasta: datetime, max_intentos: int, event_id: UUID | None = None
    ) -> Entrega | None:
        for f in self.filas:
            if f.intentos >= max_intentos or (event_id and f.aviso.event_id != event_id):
                continue
            pendiente = f.estado is EstadoEntrega.PENDIENTE and f.proximo <= ahora
            vencida = f.estado is EstadoEntrega.EN_ENVIO and f.hasta is not None and f.hasta < ahora
            if pendiente or vencida:
                f.estado, f.hasta, f.intentos = EstadoEntrega.EN_ENVIO, hasta, f.intentos + 1
                return Entrega(f.id, f.dispositivo_id, f.aviso, f.intentos, f.activo, f.token)
        return None

    def _f(self, entrega_id: UUID) -> _Fila:
        return next(f for f in self.filas if f.id == entrega_id)

    async def marcar_enviada(self, entrega_id: UUID, ahora: datetime) -> None:
        f = self._f(entrega_id)
        f.estado, f.hasta = EstadoEntrega.ENVIADO, None

    async def marcar_token_invalido(self, entrega: Entrega, ahora: datetime, motivo: str) -> None:
        f = self._f(entrega.id)
        f.estado, f.hasta, f.fallo, f.activo = EstadoEntrega.TOKEN_INVALIDO, None, motivo, False
        for otra in self.filas:
            if otra.token == f.token:
                otra.activo = False

    async def reprogramar(
        self, entrega_id: UUID, proximo_intento: datetime, codigo: str, ahora: datetime
    ) -> None:
        f = self._f(entrega_id)
        f.estado, f.hasta, f.proximo, f.fallo = (
            EstadoEntrega.PENDIENTE,
            None,
            proximo_intento,
            codigo,
        )

    async def marcar_fallida(self, entrega_id: UUID, codigo: str, ahora: datetime) -> None:
        f = self._f(entrega_id)
        f.estado, f.hasta, f.fallo = EstadoEntrega.FALLIDO, None, codigo

    async def agotar_vencidas(self, ahora: datetime, max_intentos: int) -> int:
        n = 0
        for f in self.filas:
            if (
                f.estado is EstadoEntrega.EN_ENVIO
                and f.hasta is not None
                and f.hasta < ahora
                and f.intentos >= max_intentos
            ):
                f.estado, f.hasta, f.fallo = EstadoEntrega.FALLIDO, None, "LEASE_VENCIDO"
                n += 1
        return n


@pytest.fixture
def reloj() -> Reloj:
    return Reloj()


@pytest.fixture
def repo() -> RepoMemoria:
    return RepoMemoria()


@pytest.fixture
def push() -> PushEnMemoria:
    return PushEnMemoria()


def _caso(repo: RepoMemoria, push: PushEnMemoria, reloj: Reloj, max_intentos: int = 3) -> Any:
    politica = PoliticaReintento(max_intentos, 5, 300)
    return EnviarEntregas(repo, push, reloj, politica, lease_s=60)


def _aviso() -> Aviso:
    return aviso_de_poliza_emitida(uuid4(), uuid4())


async def test_envia_una_vez_y_queda_enviada(
    repo: RepoMemoria, push: PushEnMemoria, reloj: Reloj
) -> None:
    aviso = _aviso()
    fila = repo.agregar(aviso, "t1")
    caso = _caso(repo, push, reloj)

    assert await caso.ejecutar(aviso.event_id) == 1
    assert await caso.ejecutar(aviso.event_id) == 0

    assert fila.estado is EstadoEntrega.ENVIADO
    assert push.enviados == [("t1", aviso)]


async def test_un_token_rechazado_es_terminal_y_no_afecta_al_otro_dispositivo(
    repo: RepoMemoria, push: PushEnMemoria, reloj: Reloj, caplog: pytest.LogCaptureFixture
) -> None:
    aviso = _aviso()
    malo, bueno = repo.agregar(aviso, "t-malo"), repo.agregar(aviso, "t-bueno")
    push.rechazados.add("t-malo")
    caplog.set_level(logging.INFO)

    await _caso(repo, push, reloj).ejecutar(aviso.event_id)
    reloj.t += timedelta(hours=1)
    await _caso(repo, push, reloj).ejecutar(aviso.event_id)

    assert (malo.estado, malo.activo, malo.fallo) == (
        EstadoEntrega.TOKEN_INVALIDO,
        False,
        "TOKEN_RECHAZADO",
    )
    assert bueno.estado is EstadoEntrega.ENVIADO
    assert push.intentos.count("t-malo") == 1
    assert "t-malo" not in caplog.text


async def test_un_dispositivo_ya_invalido_no_se_envia(
    repo: RepoMemoria, push: PushEnMemoria, reloj: Reloj
) -> None:
    fila = repo.agregar(_aviso(), "t-viejo", activo=False)

    await _caso(repo, push, reloj).ejecutar()

    assert fila.estado is EstadoEntrega.TOKEN_INVALIDO
    assert push.intentos == []


async def test_un_fallo_transitorio_se_reprograma_sin_dormir_y_el_barrido_lo_reintenta(
    repo: RepoMemoria, push: PushEnMemoria, reloj: Reloj
) -> None:
    aviso = _aviso()
    fila = repo.agregar(aviso, "t1")
    push.fallos["t1"] = ["TIMEOUT"]
    caso = _caso(repo, push, reloj)

    await caso.ejecutar(aviso.event_id)

    assert (fila.estado, fila.intentos, fila.fallo) == (EstadoEntrega.PENDIENTE, 1, "TIMEOUT")
    assert fila.proximo == AHORA + timedelta(seconds=5)
    assert await Barrido(caso, 10).ejecutar_una_vez() == 0

    reloj.t += timedelta(seconds=5)
    assert await Barrido(caso, 10).ejecutar_una_vez() == 1
    assert (fila.estado, fila.intentos) == (EstadoEntrega.ENVIADO, 2)
    assert len(push.enviados) == 1


async def test_agotados_los_intentos_queda_fallida_con_codigo_y_sin_reenviar(
    repo: RepoMemoria, push: PushEnMemoria, reloj: Reloj, caplog: pytest.LogCaptureFixture
) -> None:
    aviso = _aviso()
    fila = repo.agregar(aviso, "t1")
    push.fallos["t1"] = ["HTTP_503", "HTTP_503", "HTTP_503", "HTTP_503"]
    caso = _caso(repo, push, reloj, max_intentos=3)
    caplog.set_level(logging.WARNING)

    for _ in range(5):
        await caso.ejecutar()
        reloj.t += timedelta(minutes=10)

    assert (fila.estado, fila.fallo, fila.intentos) == (
        EstadoEntrega.FALLIDO,
        "REINTENTOS_AGOTADOS",
        3,
    )
    assert push.intentos == ["t1"] * 3
    assert "t1" not in caplog.text


async def test_un_error_inesperado_del_enviador_es_transitorio_y_no_filtra_el_token(
    repo: RepoMemoria, reloj: Reloj, caplog: pytest.LogCaptureFixture
) -> None:
    class Roto:
        async def enviar(self, token: str, aviso: Aviso) -> bool:
            raise RuntimeError(f"fallo con {token}")

    fila = repo.agregar(_aviso(), "t-secreto")
    caplog.set_level(logging.INFO)

    await EnviarEntregas(repo, Roto(), reloj, PoliticaReintento(), 60).ejecutar()

    assert (fila.estado, fila.fallo) == (EstadoEntrega.PENDIENTE, "ERROR_INESPERADO")
    assert "t-secreto" not in caplog.text


async def test_una_entrega_en_envio_con_lease_vigente_no_se_reclama_y_vencida_si(
    repo: RepoMemoria, push: PushEnMemoria, reloj: Reloj
) -> None:
    fila = repo.agregar(_aviso(), "t1")
    fila.estado, fila.hasta, fila.intentos = (
        EstadoEntrega.EN_ENVIO,
        AHORA + timedelta(seconds=30),
        1,
    )
    caso = _caso(repo, push, reloj)

    assert await caso.ejecutar() == 0
    reloj.t += timedelta(seconds=31)
    assert await caso.ejecutar() == 1
    assert fila.estado is EstadoEntrega.ENVIADO


async def test_una_entrega_con_lease_vencido_y_sin_intentos_pasa_a_fallida(
    repo: RepoMemoria, push: PushEnMemoria, reloj: Reloj
) -> None:
    fila = repo.agregar(_aviso(), "t1")
    fila.estado, fila.hasta, fila.intentos = EstadoEntrega.EN_ENVIO, AHORA, 3
    reloj.t += timedelta(minutes=2)

    assert await Barrido(_caso(repo, push, reloj, 3), 10).ejecutar_una_vez() == 0

    assert (fila.estado, fila.fallo) == (EstadoEntrega.FALLIDO, "LEASE_VENCIDO")
    assert push.intentos == []


def test_un_codigo_de_fallo_libre_se_rechaza() -> None:
    with pytest.raises(ValueError, match="constante"):
        FalloTransitorio("falló con el token abc")


@pytest.mark.parametrize("escenario", ["rechazado", "transitorio", "agotado", "inesperado"])
async def test_el_token_ni_su_hash_aparecen_en_logs_metricas_ni_errores(
    escenario: str,
    repo: RepoMemoria,
    reloj: Reloj,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from avisos.application.entregas import enviar as modulo_enviar
    from avisos.domain.avisos.modelos import hash_token

    token = "tok-super-secreto-123"
    push = PushEnMemoria()
    if escenario == "rechazado":
        push.rechazados.add(token)
    elif escenario in {"transitorio", "agotado"}:
        push.fallos[token] = ["HTTP_503"] * 5

    class Roto:
        async def enviar(self, token: str, aviso: Aviso) -> bool:
            raise RuntimeError(f"fallo con {token}")

    enviador: Any = Roto() if escenario == "inesperado" else push
    llamadas: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        modulo_enviar, "registrar", lambda *a, **k: llamadas.append((*a, *k.items()))
    )
    caplog.set_level(logging.DEBUG)
    fila = repo.agregar(_aviso(), token)
    caso = EnviarEntregas(repo, enviador, reloj, PoliticaReintento(2, 5, 300), 60)

    for _ in range(4 if escenario == "agotado" else 1):
        await caso.ejecutar()
        reloj.t += timedelta(minutes=10)

    prohibidos = (token, hash_token(token))
    visibles = [
        str(valor)
        for r in caplog.records
        for valor in (r.getMessage(), r.exc_text, *vars(r).values())
    ]
    visibles += [str(x) for x in llamadas] + [str(fila.fallo)]
    assert llamadas and visibles
    for prohibido in prohibidos:
        assert not any(prohibido in v for v in visibles)


async def test_cada_corrida_procesa_a_lo_sumo_un_lote(
    repo: RepoMemoria, push: PushEnMemoria, reloj: Reloj
) -> None:
    for _ in range(LOTE_MAXIMO + 1):
        repo.agregar(_aviso(), "t1")

    assert await _caso(repo, push, reloj).ejecutar() == LOTE_MAXIMO
    assert await _caso(repo, push, reloj).ejecutar() == 1
