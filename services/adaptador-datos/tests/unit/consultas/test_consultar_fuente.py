import contextlib
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest
from adaptador_datos.application.consultas.consultar_fuente import ConsultarFuente
from adaptador_datos.domain.consultas.modelos import (
    Aliado,
    CamposNoSoportados,
    ConsentimientoRequerido,
    Fuente,
    RespuestaInvalida,
)
from solventa_common.resiliencia import DependenciaNoDisponible, SinProteccion, TiempoAgotado

pytestmark = pytest.mark.anyio

CLIENTE = UUID("00000000-0000-4000-8000-000000000001")
CONSENTIMIENTO = UUID("00000000-0000-4000-9000-000000000001")
AHORA = datetime(2026, 10, 15, 15, tzinfo=UTC)
HISTORIAL = {"schemaVersion": "3.1", "confidenceScore": 0.91, "data": {"lateCount": 0, "otro": 1}}


class ProveedorFalso:
    def __init__(self, respuesta: Any = None, error: BaseException | None = None) -> None:
        self.respuesta = HISTORIAL if respuesta is None else respuesta
        self.error = error
        self.llamadas: list[tuple[Fuente, UUID, tuple[str, ...], float]] = []

    async def obtener(
        self, fuente: Fuente, cliente_id: UUID, campos_proveedor: tuple[str, ...], timeout_s: float
    ) -> Mapping[str, Any]:
        self.llamadas.append((fuente, cliente_id, campos_proveedor, timeout_s))
        if self.error is not None:
            raise self.error
        return self.respuesta


def _caso(proveedor: ProveedorFalso) -> ConsultarFuente:
    return ConsultarFuente(
        proveedores={Aliado.OPEN_FINANCE: proveedor, Aliado.DATOS_ABIERTOS: proveedor},
        proteccion=SinProteccion(),
        timeouts_ms={Aliado.OPEN_FINANCE: 700, Aliado.DATOS_ABIERTOS: 300},
        reloj=lambda: AHORA,
    )


async def _consultar(proveedor: ProveedorFalso, **cambios: Any) -> Any:
    argumentos: dict[str, Any] = {
        "fuente": Fuente.FA_HISTORIAL_PAGOS_12M,
        "cliente_id": CLIENTE,
        "consentimiento_id": CONSENTIMIENTO,
        "campos": ["moras_12m"],
        "deadline_ms": 290,
    }
    argumentos.update(cambios)
    return await _caso(proveedor).ejecutar(**argumentos)


async def test_nominal_con_linaje() -> None:
    proveedor = ProveedorFalso()
    respuesta = await _consultar(proveedor)
    assert proveedor.llamadas == [(Fuente.FA_HISTORIAL_PAGOS_12M, CLIENTE, ("lateCount",), 0.27)]
    assert respuesta.campos["moras_12m"].valor == Decimal(0)
    assert respuesta.campos_descartados == 1
    assert (respuesta.tipo, respuesta.proveedor, respuesta.version_fuente) == (
        "FINANZAS_ABIERTAS",
        "SIM-OPEN-FINANCE",
        "3.1",
    )
    assert respuesta.confianza == Decimal("0.91")
    assert respuesta.consultada_en == AHORA


async def test_usa_el_timeout_del_aliado_sin_deadline() -> None:
    proveedor = ProveedorFalso(respuesta={"version": "1.8", "nivelConfianza": 0.88, "registro": {}})
    respuesta = await _consultar(
        proveedor,
        fuente=Fuente.DA_REGISTROS_PUBLICOS,
        campos=["coincidencias_listas_restrictivas"],
        deadline_ms=None,
    )
    assert proveedor.llamadas[0][3] == 0.3
    assert respuesta.campos["coincidencias_listas_restrictivas"].estado == "NO_DISPONIBLE"


async def test_ut_w10_16_sin_consentimiento_no_llama_al_proveedor() -> None:
    proveedor = ProveedorFalso()
    with pytest.raises(ConsentimientoRequerido):
        await _consultar(proveedor, consentimiento_id=None)
    assert proveedor.llamadas == []


@pytest.mark.parametrize(
    ("campos", "invalidos"),
    [([], ()), (["moras_12m", "ingreso_mensual_estimado"], ("ingreso_mensual_estimado",))],
)
async def test_ut_w10_17_campos_no_soportados(
    campos: list[str], invalidos: tuple[str, ...]
) -> None:
    proveedor = ProveedorFalso()
    with pytest.raises(CamposNoSoportados) as error:
        await _consultar(proveedor, campos=campos)
    assert error.value.campos_invalidos == invalidos
    assert proveedor.llamadas == []


async def test_ut_w10_18_timeout() -> None:
    with pytest.raises(TiempoAgotado):
        await _consultar(ProveedorFalso(error=httpx.ReadTimeout("lento")))


async def test_ut_w10_18_5xx() -> None:
    error = httpx.HTTPStatusError(
        "503", request=httpx.Request("GET", "http://sim"), response=httpx.Response(503)
    )
    with pytest.raises(DependenciaNoDisponible):
        await _consultar(ProveedorFalso(error=error))


async def test_respuesta_invalida_se_relanza() -> None:
    with pytest.raises(RespuestaInvalida):
        await _consultar(ProveedorFalso(respuesta={"data": "no-es-objeto"}))


@pytest.mark.parametrize(
    ("proveedor", "cambios", "resultado"),
    [
        (ProveedorFalso(), {}, "ok"),
        (ProveedorFalso(), {"consentimiento_id": None}, "rechazado"),
        (ProveedorFalso(), {"campos": ["x"]}, "rechazado"),
        (ProveedorFalso(error=httpx.ReadTimeout("lento")), {}, "timeout"),
        (ProveedorFalso(error=httpx.ConnectError("caído")), {}, "error"),
        (ProveedorFalso(respuesta="malformada"), {}, "error"),
    ],
)
async def test_ut_w10_19_metrica_y_log_con_resultado(
    telemetria: Any, proveedor: ProveedorFalso, cambios: dict[str, Any], resultado: str
) -> None:
    with contextlib.suppress(Exception):
        await _consultar(proveedor, **cambios)
    puntos = [
        p
        for p in telemetria.puntos("OperationDuration")
        if p.attributes["Operation"] == "consulta.FA_HISTORIAL_PAGOS_12M"
    ]
    assert len(puntos) == 1 and puntos[0].count == 1
    errores = [
        p
        for p in telemetria.puntos("OperationErrors")
        if p.attributes["Operation"] == "consulta.FA_HISTORIAL_PAGOS_12M"
    ]
    assert errores[0].value == (1 if resultado in ("error", "timeout") else 0)
    (span,) = [s for s in telemetria.lista_spans() if s.name == "consulta.FA_HISTORIAL_PAGOS_12M"]
    assert span.attributes["result"] == resultado
    logs = [l for l in telemetria.logs() if l.get("operation") == "consulta.FA_HISTORIAL_PAGOS_12M"]
    assert any(l["result"] == resultado for l in logs)


async def test_el_log_no_contiene_valores(telemetria: Any) -> None:
    await _consultar(ProveedorFalso(respuesta={**HISTORIAL, "data": {"lateCount": 734519}}))
    (procesada,) = [l for l in telemetria.logs() if l["message"] == "consulta procesada"]
    assert procesada["fuente"] == "FA_HISTORIAL_PAGOS_12M"
    assert procesada["campos_descartados"] == 0
    assert "734519" not in telemetria.texto_logs()
    assert "734519" not in telemetria.texto_spans()
