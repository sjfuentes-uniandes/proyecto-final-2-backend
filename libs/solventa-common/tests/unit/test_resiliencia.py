import asyncio

import httpx
import pytest
from solventa_common.resiliencia import (
    CORTE_DURO_MS,
    Bulkhead,
    BulkheadLleno,
    CircuitBreaker,
    DependenciaNoDisponible,
    ProteccionDependencia,
    SinProteccion,
    TiempoAgotado,
    con_reintento,
    es_conexion_o_5xx,
    timeout_efectivo_ms,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.parametrize(
    ("configurado", "deadline", "esperado"),
    [
        (300, None, 300),  # manda el configurado
        (1000, None, CORTE_DURO_MS),  # manda el corte duro
        (700, 290, 270),  # manda el deadline menos el margen
        (700, 10, 1),  # nunca menor que 1
        (0, None, 1),
    ],
)
async def test_timeout_efectivo(configurado: int, deadline: int | None, esperado: int) -> None:
    assert timeout_efectivo_ms(configurado, deadline) == esperado


async def test_timeout_efectivo_con_margen_propio() -> None:
    assert timeout_efectivo_ms(700, 300, margen_ms=50) == 250


async def test_timeout_efectivo_con_corte_duro_propio() -> None:
    assert timeout_efectivo_ms(3000, None, corte_duro_ms=5000) == 3000
    assert timeout_efectivo_ms(9000, None, corte_duro_ms=5000) == 5000
    assert timeout_efectivo_ms(9000, 1000, corte_duro_ms=5000) == 980


async def test_sin_proteccion_usa_el_corte_duro_declarado() -> None:
    recibidos: list[float] = []

    async def llamada(timeout_s: float) -> str:
        recibidos.append(timeout_s)
        return "ok"

    assert await SinProteccion(corte_duro_ms=5000).ejecutar("F", "G", llamada, 3000, None) == "ok"
    assert await SinProteccion().ejecutar("F", "G", llamada, 3000, None) == "ok"
    assert recibidos == [3.0, 0.7]


async def test_exito_pasa_el_timeout_en_segundos() -> None:
    recibidos: list[float] = []

    async def llamada(timeout_s: float) -> str:
        recibidos.append(timeout_s)
        return "ok"

    assert await SinProteccion().ejecutar("F", "G", llamada, 300, 290) == "ok"
    assert recibidos == [0.27]


@pytest.mark.parametrize(
    "error",
    [httpx.ReadTimeout("lento"), TimeoutError()],
)
async def test_timeout_se_traduce(error: Exception) -> None:
    async def llamada(_: float) -> None:
        raise error

    with pytest.raises(TiempoAgotado):
        await SinProteccion().ejecutar("F", "G", llamada, 300, None)


async def test_llamada_que_no_respeta_su_timeout_se_corta() -> None:
    async def llamada(_: float) -> None:
        await asyncio.sleep(1)

    with pytest.raises(TiempoAgotado):
        await SinProteccion().ejecutar("F", "G", llamada, 700, 40)


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("sin conexión"),
        httpx.HTTPStatusError(
            "503",
            request=httpx.Request("GET", "http://aliado"),
            response=httpx.Response(503),
        ),
    ],
)
async def test_error_se_traduce_a_dependencia_no_disponible(error: Exception) -> None:
    async def llamada(_: float) -> None:
        raise error

    with pytest.raises(DependenciaNoDisponible) as capturado:
        await SinProteccion().ejecutar("F", "G", llamada, 300, None)
    assert (capturado.value.motivo, capturado.value.circuito) == ("ERROR", "CERRADO")


async def test_otras_excepciones_no_se_traducen() -> None:
    async def llamada(_: float) -> None:
        raise ValueError("respuesta inválida")

    with pytest.raises(ValueError):
        await SinProteccion().ejecutar("F", "G", llamada, 300, None)


# --- HU-W12: circuit breaker, bulkhead, reintento y ProteccionDependencia ---


class RelojFalso:
    def __init__(self) -> None:
        self.ahora = 1000.0

    def __call__(self) -> float:
        return self.ahora


class RespuestaInvalida(Exception):
    """Equivalente local de la excepción de dominio de adaptador-datos."""


def _http_status(codigo: int) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        str(codigo),
        request=httpx.Request("GET", "http://aliado"),
        response=httpx.Response(codigo),
    )


def _abrir(cb: CircuitBreaker, fallos: int = 5) -> None:
    for _ in range(fallos):
        cb.registrar_fallo()


async def test_ut_w12_01_cinco_fallos_abren_el_circuito() -> None:
    cb = CircuitBreaker(fallos_consecutivos=5, apertura_s=30, reloj=RelojFalso())
    _abrir(cb, 4)
    assert cb.estado == "CERRADO"
    assert cb.permitir()
    cb.registrar_fallo()
    assert cb.estado == "ABIERTO"


async def test_ut_w12_02_abierto_espera_la_apertura_y_permite_una_prueba() -> None:
    reloj = RelojFalso()
    cb = CircuitBreaker(fallos_consecutivos=5, apertura_s=30, reloj=reloj)
    _abrir(cb)
    reloj.ahora += 29.9
    assert not cb.permitir()
    assert cb.estado == "ABIERTO"
    reloj.ahora += 0.1
    assert cb.permitir()
    assert cb.estado == "SEMIABIERTO"
    assert not cb.permitir()  # una sola prueba en curso


async def test_ut_w12_03_semiabierto_exito_cierra() -> None:
    reloj = RelojFalso()
    cb = CircuitBreaker(5, 30, reloj)
    _abrir(cb)
    reloj.ahora += 30
    assert cb.permitir()
    cb.registrar_exito()
    assert cb.estado == "CERRADO"
    _abrir(cb, 4)
    assert cb.estado == "CERRADO"  # el contador volvió a 0


async def test_ut_w12_03_semiabierto_fallo_reabre_con_instante_nuevo() -> None:
    reloj = RelojFalso()
    cb = CircuitBreaker(5, 30, reloj)
    _abrir(cb)
    reloj.ahora += 30
    assert cb.permitir()
    cb.registrar_fallo()
    assert cb.estado == "ABIERTO"
    reloj.ahora += 29
    assert not cb.permitir()
    reloj.ahora += 1
    assert cb.permitir()


async def test_ut_w12_04_un_exito_reinicia_el_contador() -> None:
    cb = CircuitBreaker(5, 30, RelojFalso())
    _abrir(cb, 4)
    cb.registrar_exito()
    _abrir(cb, 4)
    assert cb.estado == "CERRADO"


async def test_semiabierto_liberar_prueba_permite_otra() -> None:
    reloj = RelojFalso()
    cb = CircuitBreaker(5, 30, reloj)
    _abrir(cb)
    reloj.ahora += 30
    assert cb.permitir()
    cb.liberar_prueba()
    assert cb.estado == "SEMIABIERTO"
    assert cb.permitir()


async def test_ut_w12_05_bulkhead_lleno_no_espera() -> None:
    bulkhead = Bulkhead(maximo=1)
    dentro = asyncio.Event()
    salir = asyncio.Event()

    async def ocupar() -> None:
        async with bulkhead.cupo():
            dentro.set()
            await salir.wait()

    tarea = asyncio.create_task(ocupar())
    await dentro.wait()
    with pytest.raises(BulkheadLleno):
        async with bulkhead.cupo():
            pass  # pragma: no cover
    salir.set()
    await tarea
    async with bulkhead.cupo():  # el cupo se liberó
        pass


@pytest.mark.parametrize(
    ("error", "esperado"),
    [
        (httpx.ConnectError("sin conexión"), True),
        (httpx.RemoteProtocolError("cortada"), True),
        (_http_status(503), True),
        (_http_status(500), True),
        (_http_status(404), False),
        (httpx.ReadTimeout("lento"), False),
        (TimeoutError(), False),
        (ValueError("otro"), False),
    ],
)
async def test_es_conexion_o_5xx(error: Exception, esperado: bool) -> None:
    assert es_conexion_o_5xx(error) is esperado


def _fallando(errores: list[Exception], llamadas: list[int]):
    async def fn() -> str:
        llamadas.append(1)
        if errores:
            raise errores.pop(0)
        return "ok"

    return fn


async def _reintentar(fn, presupuesto_s: float = 1.0) -> str:
    return await con_reintento(
        fn,
        intentos=1,
        espera_s=0.001,
        reintentable=es_conexion_o_5xx,
        presupuesto_restante_s=lambda: presupuesto_s,
        minimo_s=0.1,
    )


async def test_ut_w12_06_un_503_reintenta_una_vez() -> None:
    llamadas: list[int] = []
    assert await _reintentar(_fallando([_http_status(503)], llamadas)) == "ok"
    assert len(llamadas) == 2


async def test_ut_w12_06_el_reintento_es_acotado() -> None:
    llamadas: list[int] = []
    with pytest.raises(httpx.HTTPStatusError):
        await _reintentar(_fallando([_http_status(503), _http_status(503)], llamadas))
    assert len(llamadas) == 2


@pytest.mark.parametrize(
    "error",
    [httpx.ReadTimeout("lento"), TimeoutError(), _http_status(404)],
)
async def test_ut_w12_06_timeout_o_4xx_no_reintentan(error: Exception) -> None:
    llamadas: list[int] = []
    with pytest.raises(type(error)):
        await _reintentar(_fallando([error], llamadas))
    assert len(llamadas) == 1


async def test_ut_w12_06_sin_presupuesto_no_reintenta() -> None:
    llamadas: list[int] = []
    with pytest.raises(httpx.HTTPStatusError):
        await _reintentar(_fallando([_http_status(503)], llamadas), presupuesto_s=0.1)
    assert len(llamadas) == 1


def _proteccion(reloj: RelojFalso | None = None, bulkhead_max: int = 50) -> ProteccionDependencia:
    return ProteccionDependencia(
        fallos_consecutivos=5, apertura_s=30, bulkhead_max=bulkhead_max, reloj=reloj or RelojFalso()
    )


async def test_proteccion_exito_pasa_el_timeout_efectivo() -> None:
    recibidos: list[float] = []

    async def llamada(timeout_s: float) -> str:
        recibidos.append(timeout_s)
        return "ok"

    proteccion = _proteccion()
    assert await proteccion.ejecutar("F", "G", llamada, 300, 290) == "ok"
    assert recibidos == [pytest.approx(0.27)]
    assert proteccion.circuitos["F"].estado == "CERRADO"


async def test_ut_w12_07_timeout_cuenta_como_fallo() -> None:
    proteccion = _proteccion()

    async def lenta(_: float) -> None:
        await asyncio.sleep(1)

    for _ in range(5):
        with pytest.raises(TiempoAgotado):
            await proteccion.ejecutar("F", "G", lenta, 700, 40)
    assert proteccion.circuitos["F"].estado == "ABIERTO"


async def test_ut_w12_07_timeout_de_httpx_no_se_reintenta() -> None:
    llamadas: list[int] = []

    async def llamada(_: float) -> None:
        llamadas.append(1)
        raise httpx.ReadTimeout("lento")

    with pytest.raises(TiempoAgotado):
        await _proteccion().ejecutar("F", "G", llamada, 300, None)
    assert len(llamadas) == 1


async def test_ut_w12_08_respuesta_invalida_no_cuenta_como_fallo() -> None:
    proteccion = _proteccion()

    async def invalida(_: float) -> None:
        raise RespuestaInvalida()

    for _ in range(6):
        with pytest.raises(RespuestaInvalida):
            await proteccion.ejecutar("F", "G", invalida, 300, None)
    assert proteccion.circuitos["F"].estado == "CERRADO"


async def test_5xx_reintenta_y_luego_cuenta_un_solo_fallo() -> None:
    proteccion = _proteccion()
    llamadas: list[int] = []

    async def caida(_: float) -> None:
        llamadas.append(1)
        raise _http_status(503)

    with pytest.raises(DependenciaNoDisponible) as capturado:
        await proteccion.ejecutar("F", "G", caida, 300, None)
    assert (capturado.value.motivo, capturado.value.circuito) == ("ERROR", "CERRADO")
    assert len(llamadas) == 2
    for _ in range(3):
        with pytest.raises(DependenciaNoDisponible):
            await proteccion.ejecutar("F", "G", caida, 300, None)
    assert proteccion.circuitos["F"].estado == "CERRADO"
    with pytest.raises(DependenciaNoDisponible) as capturado:
        await proteccion.ejecutar("F", "G", caida, 300, None)
    assert capturado.value.circuito == "ABIERTO"


async def test_ut_w12_09_circuito_abierto_no_ejecuta_la_llamada() -> None:
    reloj = RelojFalso()
    proteccion = _proteccion(reloj)
    ejecutadas: list[int] = []

    async def caida(_: float) -> None:
        ejecutadas.append(1)
        raise httpx.ConnectError("sin conexión")

    for _ in range(5):
        with pytest.raises(DependenciaNoDisponible):
            await proteccion.ejecutar("F", "G", caida, 300, None)
    ejecutadas.clear()
    with pytest.raises(DependenciaNoDisponible) as capturado:
        await proteccion.ejecutar("F", "G", caida, 300, None)
    assert (capturado.value.motivo, capturado.value.circuito) == ("CIRCUITO_ABIERTO", "ABIERTO")
    assert ejecutadas == []

    async def nominal(_: float) -> str:
        return "ok"

    # El circuito es por clave: otra fuente del mismo grupo no se ve afectada.
    assert await proteccion.ejecutar("OTRA", "G", nominal, 300, None) == "ok"
    # Tras la apertura, una prueba exitosa cierra el circuito.
    reloj.ahora += 30
    assert await proteccion.ejecutar("F", "G", nominal, 300, None) == "ok"
    assert proteccion.circuitos["F"].estado == "CERRADO"


async def test_bulkhead_lleno_no_cuenta_como_fallo() -> None:
    proteccion = _proteccion(bulkhead_max=1)
    dentro = asyncio.Event()
    salir = asyncio.Event()

    async def ocupada(_: float) -> str:
        dentro.set()
        await salir.wait()
        return "ok"

    async def nominal(_: float) -> str:
        return "ok"  # pragma: no cover

    tarea = asyncio.create_task(proteccion.ejecutar("F1", "G", ocupada, 700, None))
    await dentro.wait()
    for _ in range(6):
        with pytest.raises(DependenciaNoDisponible) as capturado:
            await proteccion.ejecutar("F2", "G", nominal, 700, None)
        assert (capturado.value.motivo, capturado.value.circuito) == ("BULKHEAD", "CERRADO")
    salir.set()
    assert await tarea == "ok"
    assert proteccion.circuitos["F2"].estado == "CERRADO"


async def test_prueba_en_semiabierto_sin_veredicto_se_libera() -> None:
    reloj = RelojFalso()
    proteccion = _proteccion(reloj)

    async def caida(_: float) -> None:
        raise httpx.ConnectError("sin conexión")

    async def invalida(_: float) -> None:
        raise RespuestaInvalida()

    async def nominal(_: float) -> str:
        return "ok"

    for _ in range(5):
        with pytest.raises(DependenciaNoDisponible):
            await proteccion.ejecutar("F", "G", caida, 300, None)
    reloj.ahora += 30
    with pytest.raises(RespuestaInvalida):
        await proteccion.ejecutar("F", "G", invalida, 300, None)
    assert proteccion.circuitos["F"].estado == "SEMIABIERTO"
    assert await proteccion.ejecutar("F", "G", nominal, 300, None) == "ok"
    assert proteccion.circuitos["F"].estado == "CERRADO"
