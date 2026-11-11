"""RegistrarResultadoPago (HU-W23; DS3-25): refleja el pago en el siniestro, con su historial, en la
transacción del inbox. El primer resultado gana y nunca se reemite un evento."""

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from siniestros.application.siniestro.resultado_pago import (
    MENSAJE_DESCONOCIDO,
    MENSAJE_REPETIDO,
    ROL_SISTEMA_PAGOS,
    Reflejo,
    RegistrarResultadoPago,
)
from siniestros.domain.siniestro.modelos import EstadoSiniestro, Siniestro
from siniestros.domain.siniestro.pago import EstadoPago, ResultadoPago

pytestmark = pytest.mark.anyio
ANALISTA = UUID("a0a0a0a0-0000-4000-8000-000000000001")


def _aprobado(siniestro: Callable[..., Siniestro], ahora: datetime) -> Siniestro:
    return siniestro(
        estado=EstadoSiniestro.APROBADO,
        analista_id=ANALISTA,
        asignado_en=ahora,
        decidido_en=ahora,
        deducible=Decimal("350000.00"),
        valor_indemnizar=Decimal("2500000.00"),
    )


def _resultado(caso: Siniestro, ahora: datetime, **cambios: Any) -> ResultadoPago:
    datos: dict[str, Any] = {
        "siniestro_id": caso.id,
        "estado": EstadoPago.CONFIRMADO,
        "referencia": "IND-88-2026-0442-01",
        "transaccion_id": "TRX-IND-88-2026-0442-01",
        "motivo": None,
        "intentos": 1,
        "duplicados_descartados": 0,
        "ocurrido_en": ahora + timedelta(minutes=2),
    }
    datos.update(cambios)
    return ResultadoPago(**datos)


async def _registrar(repo: Any, resultado: ResultadoPago) -> Reflejo:
    async with repo.transaccion() as tx:
        return await RegistrarResultadoPago().registrar(resultado, tx)


async def test_la_confirmacion_guarda_el_pago_y_el_historial(
    repo: Any, siniestro: Callable[..., Siniestro], ahora: datetime, caplog: Any
) -> None:
    caso = _aprobado(siniestro, ahora)
    repo.agregar(caso)
    caplog.set_level("INFO")
    reflejo = await _registrar(repo, _resultado(caso, ahora, duplicados_descartados=2))
    assert reflejo is Reflejo.CONFIRMADO
    assert repo.bloqueados == [
        caso.id
    ]  # FOR UPDATE: serializa con la decisión y con otro resultado
    pago = repo.pagos[caso.id]
    assert (pago.estado, pago.transaccion_id, pago.intentos, pago.duplicados_descartados) == (
        EstadoPago.CONFIRMADO,
        "TRX-IND-88-2026-0442-01",
        1,
        2,
    )
    [entrada] = repo.historial
    assert (entrada.accion, entrada.siniestro_id, entrada.actor_rol) == (
        "PAGO_CONFIRMADO",
        caso.id,
        ROL_SISTEMA_PAGOS,
    )
    assert entrada.ocurrido_en == ahora + timedelta(minutes=2)  # la hora de la pasarela
    assert entrada.estado_anterior is None and entrada.estado_nuevo is None
    assert entrada.detalles == {
        "referencia": "IND-88-2026-0442-01",
        "intentos": 1,
        "duplicadosDescartados": 2,
        "transaccionId": "TRX-IND-88-2026-0442-01",
    }
    assert repo.eventos == []  # no reemite eventos
    assert repo.siniestros[caso.id] == caso  # el siniestro sigue APROBADO, sin cambios


async def test_el_rechazo_guarda_el_motivo_y_deja_el_caso_aprobado(
    repo: Any, siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    caso = _aprobado(siniestro, ahora)
    repo.agregar(caso)
    rechazo = _resultado(
        caso,
        ahora,
        estado=EstadoPago.RECHAZADO,
        transaccion_id=None,
        motivo="BENEFICIARIO_NO_HABILITADO",
    )
    assert await _registrar(repo, rechazo) is Reflejo.RECHAZADO
    assert repo.pagos[caso.id].motivo == "BENEFICIARIO_NO_HABILITADO"
    [entrada] = repo.historial
    assert entrada.accion == "PAGO_RECHAZADO"
    assert entrada.detalles is not None
    assert entrada.detalles["motivo"] == "BENEFICIARIO_NO_HABILITADO"
    assert "transaccionId" not in entrada.detalles
    assert repo.siniestros[caso.id].estado is EstadoSiniestro.APROBADO


@pytest.mark.parametrize(
    "segundo", [EstadoPago.CONFIRMADO, EstadoPago.RECHAZADO], ids=["repetido", "fuera_de_orden"]
)
async def test_un_resultado_repetido_o_fuera_de_orden_no_cambia_nada(
    repo: Any,
    siniestro: Callable[..., Siniestro],
    ahora: datetime,
    segundo: EstadoPago,
    caplog: Any,
) -> None:
    caso = _aprobado(siniestro, ahora)
    repo.agregar(caso)
    primero = _resultado(caso, ahora)
    await _registrar(repo, primero)
    caplog.set_level("INFO")
    otro = _resultado(
        caso,
        ahora,
        estado=segundo,
        transaccion_id="TRX-OTRO" if segundo is EstadoPago.CONFIRMADO else None,
        motivo=None if segundo is EstadoPago.CONFIRMADO else "OTRO_MOTIVO",
        ocurrido_en=ahora + timedelta(hours=1),
    )
    assert await _registrar(repo, otro) is Reflejo.DUPLICADO
    assert repo.pagos[caso.id] == primero  # el primero gana
    assert len(repo.historial) == 1
    assert MENSAJE_REPETIDO in caplog.text


@pytest.mark.parametrize(
    "estado",
    [
        EstadoSiniestro.REPORTADO,
        EstadoSiniestro.RECHAZADO,
    ],
)
async def test_un_siniestro_que_no_esta_aprobado_descarta_el_resultado(
    repo: Any,
    siniestro: Callable[..., Siniestro],
    ahora: datetime,
    estado: EstadoSiniestro,
    caplog: Any,
) -> None:
    extra: dict[str, Any] = {"decidido_en": ahora} if estado is EstadoSiniestro.RECHAZADO else {}
    caso = siniestro(estado=estado, **extra)
    repo.agregar(caso)
    assert await _registrar(repo, _resultado(caso, ahora)) is Reflejo.DESCONOCIDO
    assert repo.pagos == {} and repo.historial == []
    assert MENSAJE_DESCONOCIDO in caplog.text


async def test_un_siniestro_inexistente_descarta_el_resultado(
    repo: Any, siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    caso = _aprobado(siniestro, ahora)  # nunca se agrega al repositorio
    assert await _registrar(repo, _resultado(caso, ahora)) is Reflejo.DESCONOCIDO
    assert repo.pagos == {}


async def test_si_falla_la_escritura_no_queda_nada(
    repo: Any, siniestro: Callable[..., Siniestro], ahora: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    caso = _aprobado(siniestro, ahora)
    repo.agregar(caso)

    async def falla(self: Any, entrada: Any) -> None:
        raise RuntimeError("base caída")

    with pytest.raises(RuntimeError):
        async with repo.transaccion() as tx:
            monkeypatch.setattr(type(tx), "agregar_historial", falla)
            await RegistrarResultadoPago().registrar(_resultado(caso, ahora), tx)
    assert repo.pagos == {}  # la transacción se deshizo: el pago y el historial, juntos


async def test_la_medicion_y_los_logs_no_llevan_cuenta_ni_transaccion(
    telemetria: Any, repo: Any, siniestro: Callable[..., Siniestro], ahora: datetime, caplog: Any
) -> None:
    caso = _aprobado(siniestro, ahora)
    repo.agregar(caso)
    caplog.set_level("INFO")
    await _registrar(repo, _resultado(caso, ahora))
    await _registrar(repo, _resultado(caso, ahora))
    resultados = [
        s.attributes["result"]
        for s in telemetria.lista_spans()
        if s.name == "siniestro.resultado_pago"
    ]
    assert resultados == ["confirmado", "duplicado"]
    assert "TRX-IND" not in caplog.text and "****" not in caplog.text
    assert uuid4()  # sin uso: solo ids y códigos en los logs
