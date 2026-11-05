"""Dobles de prueba de siniestros (HU-W19): repositorio en memoria, almacén y reloj fijo.

El repositorio en memoria cumple los mismos puertos que `SiniestrosPg`; las pruebas de módulo
comprueban que el de verdad se comporta igual.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

import pytest
from siniestros.application.siniestro.errores import SolicitudInvalida
from siniestros.application.siniestro.puertos import RegistroIdempotencia
from siniestros.application.siniestro.vistas import (
    AccesoDenegado,
    DetalleLeido,
    FiltroBandeja,
    Indicadores,
    PaginaSiniestros,
    Plazos,
)
from siniestros.domain.siniestro.modelos import (
    EntradaHistorial,
    EstadoSiniestro,
    Evidencia,
    IntegridadEvidencia,
    Siniestro,
    TipoSoporte,
)

CURSOR_INVALIDO = "cursor-invalido"


class RelojFijo:
    def __init__(self, ahora: datetime) -> None:
        self._ahora = ahora

    def ahora(self) -> datetime:
        return self._ahora


class TxFalsa:
    def __init__(self, repo: "RepoFalso") -> None:
        self._repo = repo

    async def adquirir_idempotencia(self, clave: str) -> RegistroIdempotencia | None:
        return self._repo.idempotencia.get(clave)

    async def guardar_idempotencia(
        self, clave: str, huella: str, respuesta: dict[str, Any], creada_en: datetime
    ) -> None:
        self._repo.idempotencia[clave] = RegistroIdempotencia(huella, respuesta)

    async def siniestro(self, siniestro_id: UUID) -> Siniestro | None:
        return self._repo.siniestros.get(siniestro_id)

    async def reservar_siguiente(self) -> Siniestro | None:
        libres = [
            s
            for s in self._repo.siniestros.values()
            if s.estado is EstadoSiniestro.REPORTADO and s.analista_id is None
        ]
        return min(libres, key=lambda s: (s.reportado_en, s.id)) if libres else None

    async def guardar_asignacion(self, siniestro: Siniestro, version_anterior: int) -> None:
        self._repo.siniestros[siniestro.id] = siniestro

    async def agregar_historial(self, entrada: EntradaHistorial) -> None:
        self._repo.historial.append(entrada)

    async def evento_en_evaluacion(self, siniestro: Siniestro, ocurrido_en: datetime) -> None:
        self._repo.eventos.append(("SiniestroEnEvaluacion", siniestro.id, ocurrido_en))

    async def evento_consultado(
        self, siniestro: Siniestro, actor_id: str, ocurrido_en: datetime
    ) -> None:
        self._repo.eventos.append(("SiniestroConsultado", siniestro.id, actor_id))

    async def evento_evidencia_consultada(
        self, evidencia: Evidencia, actor_id: str, expira_en: datetime, ocurrido_en: datetime
    ) -> None:
        self._repo.eventos.append(("EvidenciaConsultada", evidencia.id, actor_id, expira_en))

    async def evento_acceso_denegado(self, acceso: AccesoDenegado, ocurrido_en: datetime) -> None:
        self._repo.eventos.append(("AccesoSiniestroDenegado", acceso))


class RepoFalso:
    """Todo en memoria. `tx_abierta` permite comprobar que nadie espera a S3 con una transacción."""

    def __init__(self) -> None:
        self.siniestros: dict[UUID, Siniestro] = {}
        self.evidencias: dict[UUID, Evidencia] = {}
        self.historial: list[EntradaHistorial] = []
        self.idempotencia: dict[str, RegistroIdempotencia] = {}
        self.eventos: list[tuple[Any, ...]] = []
        self.tx_abierta = False
        self.transacciones = 0
        self.filtros: list[FiltroBandeja] = []
        self.pagina = PaginaSiniestros((), 0, None)
        self.indicadores_: Indicadores | None = None
        self.consultas_indicadores: list[tuple[Any, ...]] = []

    def agregar(self, siniestro: Siniestro, *evidencias: Evidencia) -> None:
        self.siniestros[siniestro.id] = siniestro
        for evidencia in evidencias:
            self.evidencias[evidencia.id] = evidencia

    @asynccontextmanager
    async def transaccion(self) -> AsyncIterator[TxFalsa]:
        self.tx_abierta = True
        self.transacciones += 1
        try:
            yield TxFalsa(self)
        finally:
            self.tx_abierta = False

    async def bandeja(self, filtro: FiltroBandeja) -> PaginaSiniestros:
        self.filtros.append(filtro)
        if filtro.cursor == CURSOR_INVALIDO:
            raise SolicitudInvalida(["cursor"])
        return self.pagina

    async def indicadores(
        self,
        actor_id: UUID,
        desde: datetime,
        hasta: datetime,
        corte: datetime,
        compromiso_horas: int,
    ) -> Indicadores:
        self.consultas_indicadores.append((actor_id, desde, hasta, corte, compromiso_horas))
        assert self.indicadores_ is not None
        return self.indicadores_

    async def detalle(self, siniestro_id: UUID) -> DetalleLeido | None:
        caso = self.siniestros.get(siniestro_id)
        if caso is None:
            return None
        return DetalleLeido(
            caso,
            tuple(e for e in self.evidencias.values() if e.siniestro_id == siniestro_id),
            tuple(h for h in self.historial if h.siniestro_id == siniestro_id),
        )

    async def evidencia(
        self, siniestro_id: UUID, evidencia_id: UUID
    ) -> tuple[bool, Evidencia | None]:
        if siniestro_id not in self.siniestros:
            return False, None
        evidencia = self.evidencias.get(evidencia_id)
        if evidencia is not None and evidencia.siniestro_id != siniestro_id:
            evidencia = None
        return True, evidencia


class AlmacenFalso:
    """Integridad por clave de objeto. Cuenta las llamadas simultáneas y comprueba que ninguna corra
    con una transacción de base abierta."""

    def __init__(self, repo: RepoFalso | None = None) -> None:
        self.integridad: dict[str, IntegridadEvidencia] = {}
        self.por_omision = IntegridadEvidencia.VERIFICADA
        self.verificadas: list[str] = []
        self.firmadas: list[tuple[str, int, str]] = []
        self.enlace = "https://s3.local/objeto?X-Amz-Signature=firma-secreta"
        self.falla_al_firmar = False
        self.simultaneas = 0
        self.max_simultaneas = 0
        self.espera: asyncio.Event | None = None
        self._repo = repo

    async def verificar(self, clave: str, sha256: str) -> IntegridadEvidencia:
        if self._repo is not None:
            assert not self._repo.tx_abierta, "se esperó a S3 con una transacción abierta"
        self.simultaneas += 1
        self.max_simultaneas = max(self.max_simultaneas, self.simultaneas)
        self.verificadas.append(clave)
        try:
            await asyncio.sleep(0)
            if self.espera is not None:
                await self.espera.wait()
            return self.integridad.get(clave, self.por_omision)
        finally:
            self.simultaneas -= 1

    async def firmar_lectura(self, clave: str, ttl_s: int, tipo_contenido: str) -> str:
        from siniestros.application.siniestro.errores import DependenciaNoDisponible

        if self.falla_al_firmar:
            raise DependenciaNoDisponible("evidencias")
        self.firmadas.append((clave, ttl_s, tipo_contenido))
        return self.enlace


class SoportesFalsos:
    def __init__(self, requeridos: dict[str, tuple[TipoSoporte, ...]] | None = None) -> None:
        self.requeridos = requeridos or {
            "DANO_ACCIDENTAL": (
                TipoSoporte.FACTURA_COMPRA,
                TipoSoporte.DOCUMENTO_IDENTIDAD,
                TipoSoporte.CERTIFICADO_REPARACION,
            )
        }

    def para(self, amparo: str) -> tuple[TipoSoporte, ...]:
        return self.requeridos.get(amparo, ())


@pytest.fixture
def reloj(ahora: datetime) -> RelojFijo:
    return RelojFijo(ahora)


@pytest.fixture
def repo() -> RepoFalso:
    return RepoFalso()


@pytest.fixture
def almacen(repo: RepoFalso) -> AlmacenFalso:
    return AlmacenFalso(repo)


@pytest.fixture
def soportes() -> SoportesFalsos:
    return SoportesFalsos()


@pytest.fixture
def plazos() -> Plazos:
    return Plazos(compromiso_horas=48, alta_min=480, media_min=1440)
