"""Dobles en memoria de los puertos de la indemnización (HU-W23), con la semántica del repositorio
real: una fila por `(siniestro_id, tipo)`, la guarda de PENDIENTE al registrar un resultado y el
reclamo de la conciliación con la regla pura de `domain.indemnizacion.conciliacion`.

Con `--import-mode=importlib` un conftest no se importa: las pruebas los reciben por el fixture
`d` (un espacio de nombres con las clases y la fábrica `nueva`)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from dinero.application.indemnizacion.puertos import (
    ConsultaDesembolso,
    Registro,
    ResultadoConsulta,
    ResultadoDesembolso,
    SolicitudDesembolso,
)
from dinero.domain.indemnizacion.conciliacion import seleccionar_pendientes
from dinero.domain.indemnizacion.modelos import Indemnizacion

AHORA = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
DESPUES = AHORA + timedelta(seconds=2)
REFERENCIA = "IND-88-2026-0442-01"
BENEFICIARIO = "ben-sint-001"


def nueva(**cambios: Any) -> Indemnizacion:
    datos: dict[str, Any] = {
        "siniestro_id": uuid4(),
        "referencia": REFERENCIA,
        "monto": Decimal("2500000.00"),
        "moneda": "COP",
        "beneficiario_ref": BENEFICIARIO,
        "correlation_id": "corr-w23",
        "ahora": AHORA,
    }
    datos.update(cambios)
    return Indemnizacion.pendiente(**datos)


@dataclass
class Tx:
    nombre: str
    activa: bool = True
    confirmada: bool = False


@dataclass
class Repo:
    """Una fila por siniestro; `filas_resultado` fuerza el resultado de la guarda si no es None."""

    filas: dict[UUID, Indemnizacion] = field(default_factory=dict)
    filas_resultado: int | None = None
    resultados: list[tuple[Indemnizacion, Tx | None]] = field(default_factory=list)
    duplicados: list[UUID] = field(default_factory=list)
    reclamos: list[tuple[datetime, datetime, datetime, int]] = field(default_factory=list)
    falla_al_reclamar: Exception | None = None

    async def registrar_pendiente(
        self, indemnizacion: Indemnizacion, tx: Tx | None = None
    ) -> Registro:
        assert tx is not None and tx.activa
        existente = self.filas.get(indemnizacion.siniestro_id)
        if existente is not None:
            existente = existente.registrar_duplicado()
            self.filas[indemnizacion.siniestro_id] = existente
            return Registro(existente, nuevo=False)
        self.filas[indemnizacion.siniestro_id] = indemnizacion
        return Registro(indemnizacion, nuevo=True)

    async def registrar_resultado(self, indemnizacion: Indemnizacion, tx: Tx | None = None) -> int:
        assert tx is not None and tx.activa
        self.resultados.append((indemnizacion, tx))
        if self.filas_resultado is not None:
            return self.filas_resultado
        actual = self.filas.get(indemnizacion.siniestro_id)
        if actual is None or actual.final:
            return 0
        self.filas[indemnizacion.siniestro_id] = indemnizacion
        return 1

    async def descartar_duplicado(self, indemnizacion_id: UUID, tx: Tx | None = None) -> None:
        assert tx is not None and tx.activa
        self.duplicados.append(indemnizacion_id)

    async def reclamar(
        self, antes_de: datetime, ahora: datetime, hasta: datetime, lote: int
    ) -> list[Indemnizacion]:
        if self.falla_al_reclamar is not None:
            raise self.falla_al_reclamar
        self.reclamos.append((antes_de, ahora, hasta, lote))
        antiguedad = ahora - antes_de
        elegidas = seleccionar_pendientes(self.filas.values(), ahora, antiguedad, lote)
        reclamadas = [i.reclamar(hasta) for i in elegidas]
        for reclamada in reclamadas:
            self.filas[reclamada.siniestro_id] = reclamada
        return reclamadas


@dataclass
class Transacciones:
    falla: Exception | None = None
    abiertas: list[Tx] = field(default_factory=list)

    @asynccontextmanager
    async def transaccion(self) -> AsyncIterator[Tx]:
        if self.falla is not None:
            raise self.falla
        tx = Tx(f"resultado-{len(self.abiertas)}")
        self.abiertas.append(tx)
        try:
            yield tx
            tx.confirmada = True
        finally:
            tx.activa = False


@dataclass
class Eventos:
    agregados: list[tuple[Indemnizacion, Tx]] = field(default_factory=list)

    async def agregar(self, indemnizacion: Indemnizacion, tx: Tx) -> None:
        assert tx.activa
        self.agregados.append((indemnizacion, tx))


@dataclass
class Pagos:
    """Responde en orden lo que se le programe; el último se repite."""

    desembolsos: list[ResultadoDesembolso] = field(default_factory=list)
    consultas: list[ResultadoConsulta] = field(default_factory=list)
    solicitudes: list[SolicitudDesembolso] = field(default_factory=list)
    consultadas: list[ConsultaDesembolso] = field(default_factory=list)

    async def desembolsar(self, solicitud: SolicitudDesembolso) -> ResultadoDesembolso:
        self.solicitudes.append(solicitud)
        return self.desembolsos.pop(0) if len(self.desembolsos) > 1 else self.desembolsos[0]

    async def consultar_desembolso(self, consulta: ConsultaDesembolso) -> ResultadoConsulta:
        self.consultadas.append(consulta)
        return self.consultas.pop(0) if len(self.consultas) > 1 else self.consultas[0]


class Reloj:
    def __init__(self, *instantes: datetime) -> None:
        self.instantes = list(instantes) or [AHORA, DESPUES]

    def ahora(self) -> datetime:
        return self.instantes.pop(0) if len(self.instantes) > 1 else self.instantes[0]


@pytest.fixture
def d() -> SimpleNamespace:
    return SimpleNamespace(
        AHORA=AHORA,
        DESPUES=DESPUES,
        REFERENCIA=REFERENCIA,
        BENEFICIARIO=BENEFICIARIO,
        nueva=nueva,
        Tx=Tx,
        Repo=Repo,
        Transacciones=Transacciones,
        Eventos=Eventos,
        Pagos=Pagos,
        Reloj=Reloj,
    )
