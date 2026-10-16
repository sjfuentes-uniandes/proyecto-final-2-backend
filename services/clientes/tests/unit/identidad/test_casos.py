import contextlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from clientes.application.identidad.casos import ConsultarCliente, VerificarIdentidad
from clientes.domain.identidad.modelos import (
    ClienteNoEncontrado,
    EstadoIdentidad,
    IdentidadNoDisponible,
    ResultadoKyc,
)

pytestmark = pytest.mark.anyio

AHORA = datetime(2026, 10, 15, 15, tzinfo=UTC)
SUB = "sub-1"


class Veredicto:
    def __init__(self, resultado: ResultadoKyc, motivo: str | None = None) -> None:
        self.resultado = resultado
        self.motivo = motivo


class VerificacionFalsa:
    def __init__(self, *respuestas: Veredicto | Exception) -> None:
        self.respuestas = list(respuestas)
        self.llamadas: list[tuple[UUID, str, dict[str, Any]]] = []

    async def verificar(
        self, cliente_id: UUID, selfie: str, prueba_de_vida: dict[str, Any]
    ) -> Veredicto:
        self.llamadas.append((cliente_id, selfie, prueba_de_vida))
        respuesta = self.respuestas.pop(0)
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta


def _caso(repositorio: Any, verificacion: VerificacionFalsa) -> VerificarIdentidad:
    return VerificarIdentidad(repositorio, verificacion, reloj=lambda: AHORA)


async def test_aprobado_verifica_y_asocia_al_cliente_del_sub(repositorio: Any) -> None:
    """AC1: el resultado queda asociado al cliente correcto."""
    verificacion = VerificacionFalsa(Veredicto(ResultadoKyc.APROBADO))
    resultado = await _caso(repositorio, verificacion).ejecutar(SUB, "selfie", {"ok": True})
    assert resultado.cliente.estado is EstadoIdentidad.VERIFICADO
    assert resultado.cliente.verificado_en == AHORA
    assert repositorio.por_sub(SUB) == resultado.cliente
    assert verificacion.llamadas == [(resultado.cliente.id, "selfie", {"ok": True})]


async def test_captura_invalida_deja_pendiente_con_motivo(repositorio: Any) -> None:
    """AC2: no se marca como rechazada y se informa el motivo para repetir la captura."""
    verificacion = VerificacionFalsa(Veredicto(ResultadoKyc.CAPTURA_INVALIDA, "ILUMINACION"))
    resultado = await _caso(repositorio, verificacion).ejecutar(SUB, "selfie", {})
    assert resultado.resultado is ResultadoKyc.CAPTURA_INVALIDA
    assert resultado.motivo == "ILUMINACION"
    assert resultado.cliente.estado is EstadoIdentidad.PENDIENTE
    assert repositorio.por_sub(SUB).estado is EstadoIdentidad.PENDIENTE


async def test_rechazado_guarda_el_rechazo(repositorio: Any) -> None:
    verificacion = VerificacionFalsa(Veredicto(ResultadoKyc.RECHAZADO, "ROSTRO_NO_COINCIDE"))
    resultado = await _caso(repositorio, verificacion).ejecutar(SUB, "selfie", {})
    assert resultado.cliente.estado is EstadoIdentidad.RECHAZADO
    assert repositorio.por_sub(SUB).estado is EstadoIdentidad.RECHAZADO


async def test_error_recuperable_reintenta_con_el_mismo_cliente(repositorio: Any) -> None:
    """AC3: tras una interrupción, reintentar no crea un segundo cliente."""
    verificacion = VerificacionFalsa(
        IdentidadNoDisponible("ERROR"), Veredicto(ResultadoKyc.APROBADO)
    )
    caso = _caso(repositorio, verificacion)
    with pytest.raises(IdentidadNoDisponible):
        await caso.ejecutar(SUB, "selfie", {})
    (pendiente,) = repositorio.clientes.values()
    assert pendiente.estado is EstadoIdentidad.PENDIENTE
    resultado = await caso.ejecutar(SUB, "selfie", {})
    assert len(repositorio.clientes) == 1
    assert resultado.cliente.id == pendiente.id
    assert resultado.cliente.verificado


async def test_cliente_verificado_no_vuelve_al_proveedor(repositorio: Any) -> None:
    verificacion = VerificacionFalsa(Veredicto(ResultadoKyc.APROBADO))
    caso = _caso(repositorio, verificacion)
    primero = await caso.ejecutar(SUB, "selfie", {})
    segundo = await caso.ejecutar(SUB, "otra", {})
    assert segundo.cliente == primero.cliente
    assert segundo.resultado is ResultadoKyc.APROBADO
    assert len(verificacion.llamadas) == 1


async def test_subs_distintos_son_clientes_distintos(repositorio: Any) -> None:
    verificacion = VerificacionFalsa(
        Veredicto(ResultadoKyc.APROBADO), Veredicto(ResultadoKyc.APROBADO)
    )
    caso = _caso(repositorio, verificacion)
    uno = await caso.ejecutar("sub-a", "selfie", {})
    otro = await caso.ejecutar("sub-b", "selfie", {})
    assert uno.cliente.id != otro.cliente.id


@pytest.mark.parametrize(
    ("verificacion", "resultado"),
    [
        (VerificacionFalsa(Veredicto(ResultadoKyc.APROBADO)), "ok"),
        (VerificacionFalsa(Veredicto(ResultadoKyc.CAPTURA_INVALIDA, "X")), "rechazado"),
        (VerificacionFalsa(Veredicto(ResultadoKyc.RECHAZADO, "X")), "rechazado"),
        (VerificacionFalsa(IdentidadNoDisponible("TIEMPO_AGOTADO")), "timeout"),
        (VerificacionFalsa(IdentidadNoDisponible("ERROR")), "error"),
    ],
)
async def test_metrica_y_span_con_resultado(
    telemetria: Any, repositorio: Any, verificacion: VerificacionFalsa, resultado: str
) -> None:
    with contextlib.suppress(IdentidadNoDisponible):
        await _caso(repositorio, verificacion).ejecutar(SUB, "selfie", {})
    puntos = [
        p
        for p in telemetria.puntos("OperationDuration")
        if p.attributes["Operation"] == "identidad.verificar"
    ]
    assert len(puntos) == 1 and puntos[0].count == 1
    (span,) = [s for s in telemetria.lista_spans() if s.name == "identidad.verificar"]
    assert span.attributes["result"] == resultado


async def test_ni_la_selfie_ni_el_sub_quedan_en_logs_ni_spans(
    telemetria: Any, repositorio: Any
) -> None:
    """AC4 (back): la evidencia biométrica nunca se registra."""
    verificacion = VerificacionFalsa(Veredicto(ResultadoKyc.APROBADO))
    await _caso(repositorio, verificacion).ejecutar(
        "sub-centinela-734519", "selfie-centinela-734519", {"gesto": "centinela-734519"}
    )
    assert "procesada" in telemetria.texto_logs()
    assert "734519" not in telemetria.texto_logs()
    assert "734519" not in telemetria.texto_spans()


async def test_consultar_cliente(repositorio: Any) -> None:
    cliente = repositorio.asegurar(SUB)
    consultas = ConsultarCliente(repositorio)
    assert await consultas.identidad(cliente.id) == cliente
    assert await consultas.por_sub(SUB) == cliente


async def test_consultar_cliente_inexistente(repositorio: Any) -> None:
    consultas = ConsultarCliente(repositorio)
    with pytest.raises(ClienteNoEncontrado):
        await consultas.identidad(UUID("00000000-0000-4000-8000-0000000000ff"))
    with pytest.raises(ClienteNoEncontrado):
        await consultas.por_sub("desconocido")
    # Consultar no crea el cliente (solo la verificación lo hace).
    assert repositorio.clientes == {}
