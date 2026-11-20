"""Estado de pago por lote de los siniestros paramétricos (HU-W22, H09)."""

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from siniestros.application.siniestro.crear_parametrico import CrearParametrico
from siniestros.application.siniestro.pagos_parametricos import MAX_IDS, ConsultarPagosParametricos
from siniestros.domain.siniestro.modelos import EstadoSiniestro, OrigenSiniestro, Siniestro
from siniestros.domain.siniestro.pago import EstadoPago, ResultadoPago
from siniestros.domain.siniestro.parametrico import DatosParametrico

pytestmark = pytest.mark.anyio


async def test_el_pago_es_pendiente_hasta_que_dinero_informa_un_resultado(
    repo,
    codigos,
    reloj,
    plazos,
    datos_parametrico: Callable[..., DatosParametrico],  # type: ignore[no-untyped-def]
) -> None:
    crear = CrearParametrico(repo, codigos, reloj, plazos)
    uno = (await crear.ejecutar(*_par(datos_parametrico()))).resumen.siniestro
    otro_datos = datos_parametrico(poliza_id=uuid4())
    otro = (await crear.ejecutar(*_par(otro_datos))).resumen.siniestro
    repo.pagos[otro.id] = ResultadoPago(
        otro.id,
        EstadoPago.CONFIRMADO,
        f"IND-{otro.codigo}-01",
        "TRX-1",
        None,
        1,
        0,
        datetime(2026, 11, 5, 15, 1, tzinfo=UTC),
    )

    pagos = await ConsultarPagosParametricos(repo).ejecutar([uno.id, otro.id])

    assert {(p.siniestro_id, p.codigo, p.estado_pago) for p in pagos} == {
        (uno.id, uno.codigo, EstadoPago.PENDIENTE),
        (otro.id, otro.codigo, EstadoPago.CONFIRMADO),
    }


async def test_los_ids_inexistentes_o_de_otro_origen_se_omiten(
    repo,
    siniestro: Callable[..., Siniestro],
    ahora,  # type: ignore[no-untyped-def]
) -> None:
    movil = siniestro(
        origen=OrigenSiniestro.MOVIL,
        estado=EstadoSiniestro.APROBADO,
        decidido_en=ahora,
        valor_indemnizar=Decimal("1.00"),
    )
    repo.agregar(movil)

    assert await ConsultarPagosParametricos(repo).ejecutar([movil.id, uuid4()]) == ()


async def test_sin_ids_no_consulta_y_acota_y_deduplica_el_lote() -> None:
    class Repo:
        def __init__(self) -> None:
            self.lotes: list[tuple[object, ...]] = []

        async def pagos_parametricos(self, ids):  # type: ignore[no-untyped-def]
            self.lotes.append(tuple(ids))
            return ()

    repo = Repo()
    consulta = ConsultarPagosParametricos(repo)  # type: ignore[arg-type]
    assert await consulta.ejecutar([]) == ()
    repetido = uuid4()
    await consulta.ejecutar([repetido, repetido, *[uuid4() for _ in range(MAX_IDS + 5)]])

    assert len(repo.lotes) == 1 and len(repo.lotes[0]) == MAX_IDS
    assert repo.lotes[0].count(repetido) == 1


def _par(datos: DatosParametrico) -> tuple[str, DatosParametrico]:
    return datos.clave, datos
