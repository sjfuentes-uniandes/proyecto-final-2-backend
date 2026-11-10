"""PagarIndemnizacion (HU-W23; ADR 0015, decisiones 3 y 4), con dobles de los puertos.

- `registrar` corre en la transacción del inbox: inserta la indemnización PENDIENTE y, solo si es
  nueva, devuelve la continuación que paga (modo b). Si no es nueva, el duplicado ya quedó contado y
  no hay llamada a la pasarela.
- La continuación paga y, en otra transacción, aplica el resultado con la guarda de PENDIENTE y
  escribe el evento en el outbox, solo si cambió la fila y el estado es final. Un incierto nunca es
  un rechazo. No lanza por fallos del adaptador; solo si falla la base.
"""

from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from dinero.application.indemnizacion.pagar import (
    AprobacionInvalida,
    PagarIndemnizacion,
    SiniestroAprobado,
)
from dinero.application.indemnizacion.puertos import (
    DesembolsoConfirmado,
    DesembolsoIncierto,
    DesembolsoRechazado,
)
from dinero.domain.indemnizacion.modelos import MOTIVO_NO_INFORMADO, EstadoIndemnizacion

pytestmark = pytest.mark.anyio


def _aprobado(**cambios: Any) -> SiniestroAprobado:
    datos: dict[str, Any] = {
        "siniestro_id": uuid4(),
        "referencia": "IND-88-2026-0442-01",
        "monto": Decimal("2500000.00"),
        "moneda": "COP",
        "beneficiario_ref": "ben-sint-001",
        "correlation_id": "corr-w23",
    }
    datos.update(cambios)
    return SiniestroAprobado(**datos)


class Escenario:
    def __init__(self, d: SimpleNamespace, *desembolsos: Any) -> None:
        self.repo = d.Repo()
        self.transacciones = d.Transacciones()
        self.eventos = d.Eventos()
        self.pagos = d.Pagos(desembolsos=list(desembolsos) or [DesembolsoConfirmado("TRX-1")])
        self.reloj = d.Reloj()
        self.d = d

    def caso(self) -> PagarIndemnizacion[Any]:
        return PagarIndemnizacion(
            indemnizaciones=self.repo,
            transacciones=self.transacciones,
            eventos=self.eventos,
            pagos=self.pagos,
            reloj=self.reloj,
        )

    async def recibir(self, aprobado: SiniestroAprobado) -> Any:
        tx = self.d.Tx("inbox")
        continuacion = await self.caso().registrar(aprobado, tx)
        tx.confirmada = True
        tx.activa = False
        return continuacion


async def test_un_siniestro_nuevo_devuelve_la_continuacion_sin_pagar_todavia(d: Any) -> None:
    e = Escenario(d)
    continuacion = await e.recibir(_aprobado())
    assert continuacion is not None
    assert e.pagos.solicitudes == []  # el pago va después del commit
    [pendiente] = e.repo.filas.values()
    assert pendiente.estado is EstadoIndemnizacion.PENDIENTE and e.eventos.agregados == []


async def test_la_continuacion_confirma_y_escribe_el_evento_en_otra_transaccion(
    d: Any, logs: Any
) -> None:
    e = Escenario(d, DesembolsoConfirmado("TRX-1"))
    await (await e.recibir(_aprobado()))()
    [solicitud] = e.pagos.solicitudes
    assert (solicitud.referencia, solicitud.monto, solicitud.moneda) == (
        "IND-88-2026-0442-01",
        Decimal("2500000.00"),
        "COP",
    )
    assert (solicitud.beneficiario_ref, solicitud.correlation_id) == ("ben-sint-001", "corr-w23")
    [(resuelta, tx_resultado)] = e.repo.resultados
    assert resuelta.estado is EstadoIndemnizacion.CONFIRMADA and resuelta.transaccion_id == "TRX-1"
    assert resuelta.intentos == 1
    [(evento, tx_evento)] = e.eventos.agregados
    assert evento == resuelta and tx_evento is tx_resultado
    assert tx_resultado.confirmada and tx_resultado.nombre != "inbox"
    [info] = [r for r in logs.registros() if r["message"] == "indemnizacion: pago resuelto"]
    assert info["estado"] == "CONFIRMADA" and info["referencia"] == "IND-88-2026-0442-01"


async def test_un_rechazo_queda_rechazada_con_su_motivo_normalizado(d: Any) -> None:
    e = Escenario(d, DesembolsoRechazado(motivo="BENEFICIARIO_NO_HABILITADO", transaccion_id=None))
    await (await e.recibir(_aprobado()))()
    [(evento, _)] = e.eventos.agregados
    assert evento.estado is EstadoIndemnizacion.RECHAZADA
    assert evento.motivo == "BENEFICIARIO_NO_HABILITADO"


async def test_un_motivo_sin_forma_de_codigo_se_normaliza(d: Any) -> None:
    e = Escenario(
        d, DesembolsoRechazado(motivo="texto libre del proveedor", transaccion_id="TRX-R")
    )
    await (await e.recibir(_aprobado()))()
    [(evento, _)] = e.eventos.agregados
    assert (evento.motivo, evento.transaccion_id) == (MOTIVO_NO_INFORMADO, "TRX-R")


async def test_un_incierto_nunca_es_rechazo_y_no_escribe_evento(d: Any, logs: Any) -> None:
    e = Escenario(d, DesembolsoIncierto("ADAPTADOR_TIEMPO_AGOTADO"))
    await (await e.recibir(_aprobado()))()
    [(resuelta, _)] = e.repo.resultados
    assert resuelta.estado is EstadoIndemnizacion.PENDIENTE
    assert (resuelta.ultimo_incierto, resuelta.intentos) == ("ADAPTADOR_TIEMPO_AGOTADO", 1)
    assert e.eventos.agregados == [] and e.repo.duplicados == []
    [aviso] = logs.de_nivel("WARNING")
    assert aviso["codigo"] == "ADAPTADOR_TIEMPO_AGOTADO"


async def test_otro_evento_del_mismo_siniestro_cuenta_un_duplicado_y_no_paga(
    d: Any, logs: Any
) -> None:
    e = Escenario(d)
    aprobado = _aprobado()
    assert await e.recibir(aprobado) is not None
    assert await e.recibir(aprobado) is None
    assert await e.recibir(aprobado) is None
    [fila] = e.repo.filas.values()
    assert fila.duplicados_descartados == 2
    assert e.pagos.solicitudes == []
    ya = [r for r in logs.registros() if r["message"].startswith("indemnizacion: el siniestro ya")]
    assert len(ya) == 2 and ya[0]["estado"] == "PENDIENTE"


async def test_una_confirmacion_repetida_sobre_una_confirmada_cuenta_un_duplicado(
    d: Any, logs: Any
) -> None:
    """La conciliación se adelantó: la guarda no cambia la fila y no hay otro movimiento."""
    e = Escenario(d, DesembolsoConfirmado("TRX-1"))
    continuacion = await e.recibir(_aprobado())
    [(siniestro, fila)] = e.repo.filas.items()
    e.repo.filas[siniestro] = fila.confirmar("TRX-1", d.DESPUES)  # ya resuelta por otra tarea
    await continuacion()
    assert e.eventos.agregados == []
    assert e.repo.duplicados == [fila.indemnizacion_id]
    assert [r for r in logs.registros() if "otro proceso ya la resolvió" in r["message"]]


async def test_un_incierto_sobre_una_ya_resuelta_no_cuenta_duplicado(d: Any) -> None:
    e = Escenario(d, DesembolsoIncierto("HTTP_503"))
    continuacion = await e.recibir(_aprobado())
    [(siniestro, fila)] = e.repo.filas.items()
    e.repo.filas[siniestro] = fila.confirmar("TRX-1", d.DESPUES)
    await continuacion()
    assert e.repo.duplicados == [] and e.eventos.agregados == []


async def test_si_falla_la_base_al_registrar_el_resultado_el_error_se_propaga(d: Any) -> None:
    e = Escenario(d)
    continuacion = await e.recibir(_aprobado())
    e.transacciones.falla = RuntimeError("base caída")
    with pytest.raises(RuntimeError, match="base caída"):
        await continuacion()
    assert e.eventos.agregados == []


@pytest.mark.parametrize(
    ("cambios", "campo"),
    [
        ({"moneda": "USD"}, "moneda"),
        ({"monto": Decimal("1.234")}, "monto"),
        ({"monto": Decimal(0)}, "monto"),
        ({"referencia": "IND-1"}, "referencia"),
        ({"beneficiario_ref": ""}, "beneficiario_ref"),
    ],
)
async def test_un_siniestro_que_no_cumple_una_invariante_nombra_el_campo(
    d: Any, cambios: dict[str, Any], campo: str
) -> None:
    e = Escenario(d)
    with pytest.raises(AprobacionInvalida) as error:
        await e.recibir(_aprobado(**cambios))
    assert error.value.campo == campo
    assert e.repo.filas == {}


async def test_los_logs_no_llevan_el_beneficiario(d: Any, logs: Any) -> None:
    e = Escenario(d, DesembolsoRechazado(motivo="MOTIVO", transaccion_id=None))
    await (await e.recibir(_aprobado(beneficiario_ref="ben-sint-centinela")))()
    assert "ben-sint-centinela" not in logs.texto()


async def test_la_medicion_de_indemnizacion_pagar(telemetria: Any, d: Any) -> None:
    casos = [
        (DesembolsoConfirmado("T"), "confirmada"),
        (DesembolsoRechazado("M", None), "rechazada"),
        (DesembolsoIncierto("C"), "pendiente"),
    ]
    for resultado, esperado in casos:
        e = Escenario(d, resultado)
        await (await e.recibir(_aprobado()))()
        spans = [s for s in telemetria.lista_spans() if s.name == "indemnizacion.pagar"]
        assert spans[-1].attributes["result"] == esperado
    e = Escenario(d, DesembolsoConfirmado("T"))
    continuacion = await e.recibir(_aprobado())
    [(siniestro, fila)] = e.repo.filas.items()
    e.repo.filas[siniestro] = fila.confirmar("T", d.DESPUES)
    await continuacion()
    spans = [s for s in telemetria.lista_spans() if s.name == "indemnizacion.pagar"]
    assert spans[-1].attributes["result"] == "duplicado"
    # Sin errores: ni un rechazo ni un incierto cuentan en OperationErrors.
    assert all(p.value == 0 for p in telemetria.puntos("OperationErrors"))


async def test_el_id_de_la_indemnizacion_es_nuevo_por_siniestro(d: Any) -> None:
    e = Escenario(d)
    await e.recibir(_aprobado())
    await e.recibir(_aprobado())
    ids = {i.indemnizacion_id for i in e.repo.filas.values()}
    assert len(ids) == 2
