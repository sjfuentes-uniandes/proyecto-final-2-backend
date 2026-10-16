import contextlib
from typing import Any
from uuid import UUID

import httpx
import pytest
from adaptador_identidad.application.verificaciones.verificar_identidad import VerificarIdentidad
from adaptador_identidad.domain.verificaciones.modelos import (
    RespuestaInvalida,
    ResultadoVerificacion,
    Verificacion,
)
from solventa_common.resiliencia import DependenciaNoDisponible, SinProteccion, TiempoAgotado

pytestmark = pytest.mark.anyio

CLIENTE = UUID("00000000-0000-4000-8000-000000000001")
APROBADA = Verificacion(resultado=ResultadoVerificacion.APROBADO, motivo=None)


class ProveedorFalso:
    def __init__(
        self, respuesta: Verificacion = APROBADA, error: BaseException | None = None
    ) -> None:
        self.respuesta = respuesta
        self.error = error
        self.llamadas: list[tuple[UUID, str, dict[str, Any], float]] = []

    async def verificar(
        self, cliente_id: UUID, selfie: str, prueba_de_vida: dict[str, Any], timeout_s: float
    ) -> Verificacion:
        self.llamadas.append((cliente_id, selfie, prueba_de_vida, timeout_s))
        if self.error is not None:
            raise self.error
        return self.respuesta


def _caso(proveedor: ProveedorFalso, timeout_ms: int = 3000) -> VerificarIdentidad:
    return VerificarIdentidad(
        proveedor=proveedor, proteccion=SinProteccion(), timeout_ms=timeout_ms
    )


async def _verificar(proveedor: ProveedorFalso, **cambios: Any) -> Verificacion:
    argumentos: dict[str, Any] = {
        "cliente_id": CLIENTE,
        "selfie": "selfie-sintetica-aprobada",
        "prueba_de_vida": {"completado": True},
        "deadline_ms": 690,
    }
    argumentos.update(cambios)
    return await _caso(proveedor).ejecutar(**argumentos)


async def test_nominal() -> None:
    proveedor = ProveedorFalso()
    resultado = await _verificar(proveedor)
    assert resultado == APROBADA
    assert proveedor.llamadas == [
        (CLIENTE, "selfie-sintetica-aprobada", {"completado": True}, 0.67)
    ]


async def test_captura_invalida_se_propaga() -> None:
    invalida = Verificacion(resultado=ResultadoVerificacion.CAPTURA_INVALIDA, motivo="X")
    assert await _verificar(ProveedorFalso(respuesta=invalida)) == invalida


async def test_timeout() -> None:
    with pytest.raises(TiempoAgotado):
        await _verificar(ProveedorFalso(error=httpx.ReadTimeout("lento")))


async def test_5xx() -> None:
    error = httpx.HTTPStatusError(
        "503", request=httpx.Request("POST", "http://sim"), response=httpx.Response(503)
    )
    with pytest.raises(DependenciaNoDisponible):
        await _verificar(ProveedorFalso(error=error))


async def test_respuesta_invalida_se_relanza() -> None:
    with pytest.raises(RespuestaInvalida):
        await _verificar(ProveedorFalso(error=RespuestaInvalida("JSON")))


@pytest.mark.parametrize(
    ("proveedor", "resultado"),
    [
        (ProveedorFalso(), "ok"),
        (ProveedorFalso(error=httpx.ReadTimeout("lento")), "timeout"),
        (ProveedorFalso(error=httpx.ConnectError("caído")), "error"),
        (ProveedorFalso(error=RespuestaInvalida("JSON")), "error"),
    ],
)
async def test_metrica_y_log_con_resultado(
    telemetria: Any, proveedor: ProveedorFalso, resultado: str
) -> None:
    with contextlib.suppress(Exception):
        await _verificar(proveedor)
    puntos = [
        p
        for p in telemetria.puntos("OperationDuration")
        if p.attributes["Operation"] == "verificacion.kyc"
    ]
    assert len(puntos) == 1 and puntos[0].count == 1
    errores = [
        p
        for p in telemetria.puntos("OperationErrors")
        if p.attributes["Operation"] == "verificacion.kyc"
    ]
    assert errores[0].value == (1 if resultado in ("error", "timeout") else 0)
    (span,) = [s for s in telemetria.lista_spans() if s.name == "verificacion.kyc"]
    assert span.attributes["result"] == resultado


async def test_el_log_no_contiene_la_selfie(telemetria: Any) -> None:
    await _verificar(ProveedorFalso(), selfie="selfie-con-datos-sensibles-734519")
    assert "734519" not in telemetria.texto_logs()
    assert "734519" not in telemetria.texto_spans()
