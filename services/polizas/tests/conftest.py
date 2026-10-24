"""Dobles y datos de las pruebas de polizas (HU-W13).

Los insumos salen de contracts/datos-sinteticos/cotizaciones-suscripcion.json y las reglas, de la
semilla que publica catalogo (2026.10.1): las pruebas evalúan exactamente lo que se despliega. El
reloj es el `ahoraDeReferencia` de los datos sintéticos.
"""

import asyncio
import dataclasses
import io
import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from polizas.domain.suscripcion.errores import (
    CotizacionNoEncontrada,
    ReglasNoVigentes,
)
from polizas.domain.suscripcion.insumos import huella, insumos_de
from polizas.domain.suscripcion.modelos import (
    Actor,
    Alternativa,
    CasoBandeja,
    Decision,
    DescriptorAlternativa,
    FiltrosBandeja,
    InsumosSuscripcion,
    Regla,
    ReglasSuscripcion,
    ResultadoDecision,
    RolActor,
    TipoRegla,
    Visibilidad,
)
from solventa_common import telemetry
from solventa_common.logging import FormateadorJson
from solventa_common.settings import ServiceSettings

if TYPE_CHECKING:
    from polizas.main import Puertos

RAIZ = Path(__file__).resolve().parents[3]
DATOS = json.loads(
    (RAIZ / "contracts/datos-sinteticos/cotizaciones-suscripcion.json").read_text(encoding="utf-8")
)
SEMILLA_REGLAS = (
    RAIZ
    / "services/catalogo/src/catalogo/adapters/outbound/semillas/reglas_suscripcion_2026_10_1.json"
)
AHORA = datetime.fromisoformat(DATOS["ahoraDeReferencia"])
ASESOR = Actor("sub-asesor-1", RolActor.ASESOR)
VERSION_REGLAS = DATOS["reglas"]["version"]


def caso(id_caso: str) -> dict[str, Any]:
    caso_: dict[str, Any] = next(c for c in DATOS["casos"] if c["id"] == id_caso)
    return caso_


def insumos(id_caso: str) -> InsumosSuscripcion:
    return insumos_de(caso(id_caso)["insumos"])


def definicion_semilla() -> dict[str, Any]:
    definicion: dict[str, Any] = json.loads(SEMILLA_REGLAS.read_text(encoding="utf-8"))
    return definicion


def regla_de(datos: dict[str, Any]) -> Regla:
    valor = datos["valor"]
    return Regla(
        codigo=datos["codigo"],
        orden=datos["orden"],
        tipo=TipoRegla(datos["tipo"]),
        descripcion=datos["descripcion"],
        campo=datos["campo"],
        operador=datos["operador"],
        valor=valor if isinstance(valor, str) else tuple(valor),
        visibilidad=Visibilidad(datos["visibilidad"]),
        motivo_cliente=datos["motivoCliente"],
    )


def reglas_semilla() -> ReglasSuscripcion:
    """Las reglas 2026.10.1 con la misma huella que publica catalogo (hash canónico)."""
    definicion = definicion_semilla()
    return ReglasSuscripcion(
        producto_id=definicion["productoId"],
        version=VERSION_REGLAS,
        hash=huella(definicion),
        reglas=tuple(regla_de(r) for r in definicion["reglas"]),
        alternativas=tuple(
            Alternativa(a["productoId"], DescriptorAlternativa(**a["descriptor"]))
            for a in definicion["alternativas"]
        ),
    )


class RelojFijo:
    def __init__(self, instante: datetime = AHORA) -> None:
        self.instante = instante

    def ahora(self) -> datetime:
        return self.instante


@dataclass
class InsumosDoble:
    """InsumosOfertaPuerto: los casos sintéticos por cotizacionId, o la falla configurada."""

    casos: dict[UUID, InsumosSuscripcion] = field(default_factory=dict)
    falla: Exception | None = None
    demora_s: float = 0.0
    llamadas: list[tuple[UUID, Actor]] = field(default_factory=list)

    async def insumos(self, cotizacion_id: UUID, actor: Actor) -> InsumosSuscripcion:
        self.llamadas.append((cotizacion_id, actor))
        if self.demora_s:
            await asyncio.sleep(self.demora_s)
        if self.falla is not None:
            raise self.falla
        if cotizacion_id not in self.casos:
            raise CotizacionNoEncontrada()
        return self.casos[cotizacion_id]


@dataclass
class ReglasDoble:
    """ReglasSuscripcionPuerto con la semilla de catalogo, o la falla configurada."""

    versiones: dict[str, ReglasSuscripcion] = field(default_factory=dict)
    vigente: str | None = VERSION_REGLAS
    falla: Exception | None = None
    demora_s: float = 0.0

    def __post_init__(self) -> None:
        self.versiones.setdefault(VERSION_REGLAS, reglas_semilla())

    async def vigentes(self, producto_id: str) -> ReglasSuscripcion:
        return await self._dar(self.vigente)

    async def por_version(self, producto_id: str, version: str) -> ReglasSuscripcion:
        return await self._dar(version)

    async def _dar(self, version: str | None) -> ReglasSuscripcion:
        if self.demora_s:
            await asyncio.sleep(self.demora_s)
        if self.falla is not None:
            raise self.falla
        if version is None or version not in self.versiones:
            raise ReglasNoVigentes()
        return self.versiones[version]


class DecisionesMemoria:
    """DecisionesRepositorio en memoria, con la unicidad de la tabla (cotización, clave, código)."""

    def __init__(self) -> None:
        self.decisiones: list[Decision] = []
        self.guardar_falla: bool = False

    async def guardar(self, decision: Decision) -> bool:
        if self.guardar_falla or any(
            d.cotizacion_id == decision.cotizacion_id
            or d.idempotency_key == decision.idempotency_key
            or d.codigo == decision.codigo
            for d in self.decisiones
        ):
            return False
        self.decisiones.append(decision)
        return True

    async def por_id(self, decision_id: UUID) -> Decision | None:
        return next((d for d in self.decisiones if d.id == decision_id), None)

    async def por_cotizacion(self, cotizacion_id: UUID) -> Decision | None:
        return next((d for d in self.decisiones if d.cotizacion_id == cotizacion_id), None)

    async def por_idempotencia(self, clave: UUID) -> Decision | None:
        return next((d for d in self.decisiones if d.idempotency_key == clave), None)

    def _en_revision(self, filtros: FiltrosBandeja) -> list[Decision]:
        def pasa(d: Decision) -> bool:
            q = (filtros.q or "").upper().removeprefix("COT-")
            return (
                d.resultado is ResultadoDecision.REVISION_REQUERIDA
                and filtros.producto_id in (None, d.producto_id)
                and (filtros.motivo is None or filtros.motivo in d.evaluacion.motivos)
                and (
                    not filtros.q
                    or d.codigo.startswith((filtros.q or "").upper())
                    or str(d.cotizacion_id).upper().startswith(q)
                )
            )

        return sorted(filter(pasa, self.decisiones), key=lambda d: (d.decidida_en, d.id))

    async def bandeja(
        self, filtros: FiltrosBandeja, limite: int, despues: tuple[datetime, UUID] | None
    ) -> list[CasoBandeja]:
        casos = [
            CasoBandeja(
                decision_id=d.id,
                codigo=d.codigo,
                cotizacion_id=d.cotizacion_id,
                cotizacion_codigo=d.cotizacion_codigo,
                cliente_id=d.cliente_id,
                producto_id=d.producto_id,
                moneda=d.insumos.moneda,
                suma_asegurada=d.insumos.suma_asegurada,
                motivos=d.evaluacion.motivos,
                reglas_no_cumplidas=d.evaluacion.reglas_no_cumplidas,
                decidida_en=d.decidida_en,
            )
            for d in self._en_revision(filtros)
            if despues is None or (d.decidida_en, d.id) > despues
        ]
        return casos[:limite]

    async def contar_bandeja(self, filtros: FiltrosBandeja) -> int:
        return len(self._en_revision(filtros))

    async def contar_dia(self, desde: datetime, hasta: datetime) -> tuple[int, int, float | None]:
        del_dia = [d for d in self.decisiones if desde <= d.decidida_en < hasta]
        revision = [d for d in del_dia if d.resultado is ResultadoDecision.REVISION_REQUERIDA]
        media = sum(d.duracion_ms for d in del_dia) / len(del_dia) if del_dia else None
        return len(del_dia) - len(revision), len(revision), media

    async def contar_fuera_del_compromiso(self, antes_de: datetime) -> int:
        return sum(
            1
            for d in self.decisiones
            if d.resultado is ResultadoDecision.REVISION_REQUERIDA and d.decidida_en < antes_de
        )


class CodigosMemoria:
    def __init__(self) -> None:
        self.numero = 0

    async def siguiente(self, anio: int) -> str:
        self.numero += 1
        return f"DEC-{anio:04d}-{self.numero:06d}"


@dataclass
class Dobles:
    insumos: InsumosDoble
    reglas: ReglasDoble
    decisiones: DecisionesMemoria
    codigos: CodigosMemoria
    reloj: RelojFijo

    def puertos(self) -> "Puertos":
        # Importación diferida: las pruebas del dominio no necesitan la app.
        from polizas.main import Puertos

        return Puertos(self.insumos, self.reglas, self.decisiones, self.codigos, self.reloj)


@dataclass(frozen=True)
class Sinteticos:
    """Acceso a los datos sintéticos y a la semilla de reglas desde las pruebas (fixture)."""

    datos: dict[str, Any] = field(default_factory=lambda: DATOS)
    ahora: datetime = AHORA

    def caso(self, id_caso: str) -> dict[str, Any]:
        return caso(id_caso)

    def insumos(self, id_caso: str) -> InsumosSuscripcion:
        return insumos(id_caso)

    def reglas(self) -> ReglasSuscripcion:
        return reglas_semilla()

    def regla(self, codigo: str) -> Regla:
        return next(r for r in reglas_semilla().reglas if r.codigo == codigo)

    def cotizacion_id(self, id_caso: str) -> str:
        return cotizacion_id(id_caso)

    def respuesta_reglas(self) -> dict[str, Any]:
        """El cuerpo que responde catalogo para las reglas 2026.10.1 (ReglasSuscripcionRespuesta)."""
        definicion = definicion_semilla()
        return {
            "productoId": definicion["productoId"],
            "version": VERSION_REGLAS,
            "vigenteDesde": "2026-10-01T00:00:00Z",
            "hash": huella(definicion),
            "definicion": definicion,
        }


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """`id_caso`: todos los casos sintéticos; `id_caso_decidido`: los que llegan a decisión."""
    if "id_caso" in metafunc.fixturenames:
        metafunc.parametrize("id_caso", [c["id"] for c in DATOS["casos"]])
    if "id_caso_decidido" in metafunc.fixturenames:
        decididos = [c["id"] for c in DATOS["casos"] if "resultado" in c["esperado"]]
        metafunc.parametrize("id_caso_decidido", decididos)


@pytest.fixture
def sinteticos() -> Sinteticos:
    return Sinteticos()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def dobles() -> Dobles:
    """Todos los casos sintéticos disponibles en el doble de cotizacion."""
    casos = {insumos(c["id"]).cotizacion_id: insumos(c["id"]) for c in DATOS["casos"]}
    return Dobles(
        InsumosDoble(casos), ReglasDoble(), DecisionesMemoria(), CodigosMemoria(), RelojFijo()
    )


@pytest.fixture
def api(dobles: Dobles) -> Iterator[TestClient]:
    """App con los dobles, con su lifespan arrancado."""
    from polizas.config import Settings
    from polizas.main import crear_app

    with TestClient(crear_app(Settings(), puertos=dobles.puertos())) as cliente:
        yield cliente


def cabeceras(rol: str = "ASESOR", actor: str = "sub-asesor-1", **otras: str) -> dict[str, str]:
    return {"X-Actor-Id": actor, "X-Actor-Rol": rol, **otras}


def cotizacion_id(id_caso: str) -> str:
    cotizacion: str = caso(id_caso)["insumos"]["cotizacionId"]
    return cotizacion


def vencido(id_caso: str, en: datetime) -> InsumosSuscripcion:
    return dataclasses.replace(insumos(id_caso), vigente_hasta=en)


# --- Telemetría y logs (como cotizacion) -----------------------------------------------------


@dataclass
class Telemetria:
    lector: InMemoryMetricReader
    salida: io.StringIO

    def texto_logs(self) -> str:
        return self.salida.getvalue()

    def logs(self) -> list[dict[str, Any]]:
        return [json.loads(linea) for linea in self.salida.getvalue().splitlines() if linea.strip()]

    def resultados(self, operacion: str) -> list[str]:
        """Resultado de cada medición de la operación (log de `telemetry.registrar`)."""
        return [
            log["result"]
            for log in self.logs()
            if log.get("operation") == operacion and log["message"].startswith("operación ")
        ]

    def mediciones(self, operacion: str) -> int:
        """Puntos de OperationDuration con la dimensión Operation (métrica de la operación)."""
        datos = self.lector.get_metrics_data()
        if datos is None:
            return 0
        return sum(
            punto.count
            for recurso in datos.resource_metrics
            for alcance in recurso.scope_metrics
            for metrica in alcance.metrics
            if metrica.name == "OperationDuration"
            for punto in metrica.data.data_points
            if dict(punto.attributes or {}).get("Operation") == operacion
        )


@pytest.fixture
def capturar_logs() -> Iterator[Callable[..., io.StringIO]]:
    """Redirige el logger raíz a un buffer con el formateador JSON."""
    raiz = logging.getLogger()
    anteriores, nivel = list(raiz.handlers), raiz.level

    def capturar(servicio: str = "polizas", ambiente: str = "pruebas") -> io.StringIO:
        salida = io.StringIO()
        handler = logging.StreamHandler(salida)
        handler.setFormatter(FormateadorJson(servicio, ambiente))
        raiz.handlers = [handler]
        raiz.setLevel(logging.INFO)
        return salida

    yield capturar
    raiz.handlers, raiz.level = anteriores, nivel


@pytest.fixture
def telemetria(
    monkeypatch: pytest.MonkeyPatch, capturar_logs: Callable[..., io.StringIO]
) -> Telemetria:
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    lector = InMemoryMetricReader()
    telemetry.configurar(
        ServiceSettings(service_name="polizas", environment="pruebas"),
        span_exporter=InMemorySpanExporter(),
        metric_readers=[lector],
    )
    return Telemetria(lector=lector, salida=capturar_logs())
