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
from siniestros.application.siniestro.puertos import DecisionFinalDuplicada, RegistroIdempotencia
from siniestros.application.siniestro.vistas import (
    AccesoDenegado,
    DecisionRegistrada,
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

    async def bloquear_siniestro(self, siniestro_id: UUID) -> Siniestro | None:
        self._repo.bloqueados.append(siniestro_id)
        return self._repo.siniestros.get(siniestro_id)

    async def ids_evidencias(self, siniestro_id: UUID) -> frozenset[UUID]:
        return frozenset(
            e.id for e in self._repo.evidencias.values() if e.siniestro_id == siniestro_id
        )

    async def reservar_plazos_vencidos(self, ahora: datetime, lote: int) -> list[Siniestro]:
        vencidos = sorted(
            (
                s
                for s in self._repo.siniestros.values()
                if s.estado is EstadoSiniestro.FALTA_SOPORTE
                and s.plazo_soporte_vence_en is not None
                and s.plazo_soporte_vence_en < ahora
            ),
            key=lambda s: (s.plazo_soporte_vence_en, s.id),
        )
        return vencidos[:lote]

    async def guardar_transicion(self, siniestro: Siniestro, version_anterior: int) -> None:
        if self._repo.siniestros[siniestro.id].version != version_anterior:
            raise RuntimeError("guardar_transicion: el siniestro cambió de versión")
        self._repo.siniestros[siniestro.id] = siniestro

    async def registrar_decision(self, decision: DecisionRegistrada) -> None:
        finales = [
            d
            for d in self._repo.decisiones
            if d.siniestro_id == decision.siniestro_id and d.tipo in ("APROBAR", "RECHAZAR")
        ]
        if self._repo.decision_perdida_en_carrera or (
            decision.tipo in ("APROBAR", "RECHAZAR") and finales
        ):
            raise DecisionFinalDuplicada
        self._repo.decisiones.append(decision)

    async def agregar_historial(self, entrada: EntradaHistorial) -> None:
        self._repo.historial.append(entrada)

    async def evento_en_evaluacion(
        self, siniestro: Siniestro, ocurrido_en: datetime, motivo: str = "ASIGNACION"
    ) -> None:
        self._repo.eventos.append(("SiniestroEnEvaluacion", siniestro.id, ocurrido_en, motivo))

    async def evento_aprobado(
        self, siniestro: Siniestro, referencia_pago: str, decidido_por: UUID, aprobado_en: datetime
    ) -> None:
        self._repo.eventos.append(
            ("SiniestroAprobado", siniestro.id, referencia_pago, decidido_por, aprobado_en)
        )

    async def evento_rechazado(
        self, siniestro: Siniestro, causal: Any, decidido_por: UUID, decidido_en: datetime
    ) -> None:
        self._repo.eventos.append(("SiniestroRechazado", siniestro.id, causal, decidido_por))

    async def evento_soporte_solicitado(
        self,
        siniestro: Siniestro,
        documentos: Any,
        canal: Any,
        decidido_por: UUID,
        ocurrido_en: datetime,
    ) -> None:
        self._repo.eventos.append(
            (
                "SiniestroSoporteSolicitado",
                siniestro.id,
                documentos,
                canal,
                decidido_por,
                siniestro.plazo_soporte_vence_en,
            )
        )

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
        self.bloqueados: list[UUID] = []
        self.decisiones: list[DecisionRegistrada] = []
        self.decision_perdida_en_carrera = False
        self.liquidaciones: dict[UUID, Any] = {}
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
        """Como una transacción real: si algo falla, no queda nada escrito."""
        copia = (
            dict(self.siniestros),
            list(self.historial),
            list(self.eventos),
            list(self.decisiones),
            dict(self.idempotencia),
        )
        self.tx_abierta = True
        self.transacciones += 1
        try:
            yield TxFalsa(self)
        except BaseException:
            self.siniestros, self.historial, self.eventos, self.decisiones, self.idempotencia = (
                copia[0],
                copia[1],
                copia[2],
                copia[3],
                copia[4],
            )
            raise
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
            self.liquidaciones.get(siniestro_id),
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
