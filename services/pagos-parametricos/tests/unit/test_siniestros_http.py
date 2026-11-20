"""La ACL hacia `siniestros` (HU-W22, W22-B08): encabezados, cuerpo, respuestas definitivas, timeout,
5xx con reintento acotado, circuito y bulkhead."""

import asyncio
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest
from pagos_parametricos.adapters.outbound.siniestros_http import (
    SiniestrosHttp,
    cuerpo_de,
)
from pagos_parametricos.domain.parametrico.activacion import SolicitudSiniestro, clave_idempotente
from pagos_parametricos.domain.parametrico.errores import SiniestrosRechazo
from pagos_parametricos.domain.parametrico.modelos import EventoObservado
from solventa_common.resiliencia import (
    DependenciaNoDisponible,
    ProteccionDependencia,
    TiempoAgotado,
)

pytestmark = pytest.mark.anyio
EVENTO = UUID("bafc2b26-7c88-5ae3-b5ac-26fdf5427a09")
POLIZA = UUID("b2100000-0000-4000-8000-000000000101")
CLIENTE = UUID("00000000-0000-4000-8000-000000000002")
SINIESTRO = "5a000000-0000-4000-8000-000000000022"


def _solicitud() -> SolicitudSiniestro:
    return SolicitudSiniestro(
        clave=clave_idempotente(EVENTO, POLIZA),
        evento_id=EVENTO,
        poliza_id=POLIZA,
        cliente_id=CLIENTE,
        numero="250-2026-000101",
        ramo="Paramétrico",
        vigencia_desde=date(2026, 9, 1),
        vigencia_hasta=date(2027, 8, 31),
        moneda="COP",
        evento=EventoObservado(
            EVENTO,
            "IDEAM-SIM",
            "IDEAM-1",
            "EVT-2026-0913",
            "LLUVIA_ACUMULADA",
            Decimal(135),
            "mm",
            "11001",
            datetime(2026, 11, 10, 14, 0, tzinfo=UTC),
        ),
        umbral="120.000",
        regla="VALOR_IGUAL_O_SUPERIOR_AL_UMBRAL",
        version_regla="1",
        valor="1400000.00",
    )


def _resumen() -> dict[str, Any]:
    return {"id": SINIESTRO, "codigo": "88-2026-1001", "estado": "APROBADO", "extra": "ignorado"}


def _adaptador(
    manejador: Any, *, proteccion: ProteccionDependencia | None = None, timeout_ms: int = 600
) -> tuple[SiniestrosHttp, list[httpx.Request]]:
    recibidas: list[httpx.Request] = []

    def atender(request: httpx.Request) -> httpx.Response:
        recibidas.append(request)
        return manejador(request)  # type: ignore[no-any-return]

    cliente = httpx.AsyncClient(transport=httpx.MockTransport(atender))
    return (
        SiniestrosHttp(
            cliente,
            "http://siniestros:8080/",
            timeout_ms,
            proteccion or ProteccionDependencia(5, 30, 10),
        ),
        recibidas,
    )


async def test_201_es_un_siniestro_creado_y_lleva_la_clave_el_actor_y_el_cuerpo_del_contrato() -> (
    None
):
    adaptador, recibidas = _adaptador(lambda r: httpx.Response(201, json=_resumen()))

    creado = await adaptador.crear(_solicitud())

    assert (str(creado.siniestro_id), creado.codigo, creado.creado) == (
        SINIESTRO,
        "88-2026-1001",
        True,
    )
    [r] = recibidas
    assert (r.method, str(r.url)) == ("POST", "http://siniestros:8080/v1/siniestros/parametricos")
    assert r.headers["Idempotency-Key"] == f"{EVENTO}:{POLIZA}"
    assert (r.headers["X-Actor-Id"], r.headers["X-Actor-Rol"]) == (
        "pagos-parametricos",
        "SISTEMA_PARAMETRICO",
    )
    assert json.loads(r.content) == cuerpo_de(_solicitud())


async def test_el_cuerpo_es_el_del_pacto_con_siniestros() -> None:
    pacto = json.loads(
        (
            __import__("pathlib").Path(__file__).parents[4]
            / "contracts/pact/pagos-parametricos-siniestros.json"
        ).read_text(encoding="utf-8")
    )
    esperado = pacto["interactions"][0]["request"]["body"]["content"]

    assert cuerpo_de(_solicitud()) == esperado | {
        "evento": esperado["evento"] | {"umbral": "120.000"}
    }


async def test_200_es_el_siniestro_que_ya_existia() -> None:
    adaptador, _ = _adaptador(lambda r: httpx.Response(200, json=_resumen()))

    creado = await adaptador.crear(_solicitud())

    assert creado.creado is False and str(creado.siniestro_id) == SINIESTRO


@pytest.mark.parametrize(
    ("estado", "cuerpo", "codigo"),
    [
        (422, {"codigo": "IDEMPOTENCIA_CONFLICTO", "mensaje": "x"}, "IDEMPOTENCIA_CONFLICTO"),
        (403, {"codigo": "ROL_NO_AUTORIZADO"}, "ROL_NO_AUTORIZADO"),
        (401, {"mensaje": "sin codigo"}, None),
        (404, None, None),
        (202, _resumen(), None),
    ],
)
async def test_un_4xx_o_un_estado_que_no_se_promete_es_un_rechazo_con_su_codigo(
    estado: int, cuerpo: Any, codigo: str | None
) -> None:
    adaptador, _ = _adaptador(
        lambda r: httpx.Response(estado, json=cuerpo) if cuerpo else httpx.Response(estado)
    )

    with pytest.raises(SiniestrosRechazo) as error:
        await adaptador.crear(_solicitud())

    assert (error.value.estado, error.value.codigo) == (estado, codigo)


@pytest.mark.parametrize(
    "cuerpo",
    [{"id": "no-uuid", "codigo": "88-2026-1001"}, {"id": SINIESTRO, "codigo": "X"}, {"otro": 1}],
)
async def test_un_200_fuera_de_contrato_es_un_rechazo_y_no_un_siniestro(
    cuerpo: dict[str, Any],
) -> None:
    adaptador, _ = _adaptador(lambda r: httpx.Response(200, json=cuerpo))

    with pytest.raises(SiniestrosRechazo):
        await adaptador.crear(_solicitud())


async def test_un_5xx_se_reintenta_una_vez_y_si_sigue_es_dependencia_no_disponible() -> None:
    adaptador, recibidas = _adaptador(lambda r: httpx.Response(503, json={"codigo": "X"}))

    with pytest.raises(DependenciaNoDisponible):
        await adaptador.crear(_solicitud())

    assert len(recibidas) == 2  # el original y un reintento acotado, con la misma clave
    assert {r.headers["Idempotency-Key"] for r in recibidas} == {f"{EVENTO}:{POLIZA}"}


async def test_el_reintento_de_un_5xx_puede_recuperarse() -> None:
    respuestas = iter([httpx.Response(502), httpx.Response(201, json=_resumen())])
    adaptador, recibidas = _adaptador(lambda r: next(respuestas))

    creado = await adaptador.crear(_solicitud())

    assert creado.creado is True and len(recibidas) == 2


async def test_una_conexion_caida_es_dependencia_no_disponible() -> None:
    def caida(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sin ruta")

    adaptador, _ = _adaptador(caida)

    with pytest.raises(DependenciaNoDisponible):
        await adaptador.crear(_solicitud())


async def test_sin_respuesta_a_tiempo_es_tiempo_agotado() -> None:
    async def lento(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(201, json=_resumen())

    cliente = httpx.AsyncClient(transport=httpx.MockTransport(lento))
    adaptador = SiniestrosHttp(cliente, "http://siniestros", 50, ProteccionDependencia(5, 30, 10))

    with pytest.raises(TiempoAgotado):
        await adaptador.crear(_solicitud())


async def test_el_timeout_nunca_supera_el_corte_duro_de_700_ms() -> None:
    adaptador, _ = _adaptador(lambda r: httpx.Response(201, json=_resumen()), timeout_ms=5000)

    async def lento(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(201, json=_resumen())

    adaptador._cliente = httpx.AsyncClient(transport=httpx.MockTransport(lento))  # type: ignore[attr-defined]
    inicio = asyncio.get_event_loop().time()
    with pytest.raises(TiempoAgotado):
        await adaptador.crear(_solicitud())

    assert asyncio.get_event_loop().time() - inicio < 0.9


async def test_el_circuito_se_abre_tras_fallos_seguidos_y_no_llama() -> None:
    adaptador, recibidas = _adaptador(
        lambda r: httpx.Response(500), proteccion=ProteccionDependencia(2, 30, 10)
    )
    for _ in range(2):
        with pytest.raises(DependenciaNoDisponible):
            await adaptador.crear(_solicitud())
    llamadas = len(recibidas)

    with pytest.raises(DependenciaNoDisponible) as error:
        await adaptador.crear(_solicitud())

    assert error.value.motivo == "CIRCUITO_ABIERTO" and len(recibidas) == llamadas


async def test_el_bulkhead_lleno_no_espera_un_cupo() -> None:
    liberar = asyncio.Event()

    async def retenida(request: httpx.Request) -> httpx.Response:
        await liberar.wait()
        return httpx.Response(201, json=_resumen())

    cliente = httpx.AsyncClient(transport=httpx.MockTransport(retenida))
    adaptador = SiniestrosHttp(cliente, "http://siniestros", 600, ProteccionDependencia(5, 30, 2))
    primeras = [asyncio.create_task(adaptador.crear(_solicitud())) for _ in range(2)]
    await asyncio.sleep(0.02)

    with pytest.raises(DependenciaNoDisponible) as error:
        await adaptador.crear(_solicitud())

    assert error.value.motivo == "BULKHEAD"
    liberar.set()
    assert all(c.creado for c in await asyncio.gather(*primeras))


async def test_la_correlacion_se_propaga_si_el_cliente_es_el_de_la_libreria() -> None:
    from solventa_common import correlation

    recibidas: list[httpx.Request] = []

    def atender(request: httpx.Request) -> httpx.Response:
        recibidas.append(request)
        return httpx.Response(201, json=_resumen())

    async with correlation.cliente_async(transport=httpx.MockTransport(atender)) as cliente:
        token = correlation.establecer("corr-w22-http")
        try:
            await SiniestrosHttp(
                cliente, "http://siniestros", 600, ProteccionDependencia(5, 30, 10)
            ).crear(_solicitud())
        finally:
            correlation.restablecer(token)

    assert recibidas[0].headers["X-Correlation-Id"] == "corr-w22-http"
