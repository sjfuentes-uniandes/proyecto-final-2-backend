"""Casos de uso y adaptador HTTP de dispositivos (HU-M11)."""

import json
from uuid import UUID

import httpx
import pytest
from bff_movil.adapters.outbound.dispositivos_http import DispositivosHttp
from bff_movil.application.dispositivos.casos import DarDeBajaDispositivo, RegistrarDispositivo
from bff_movil.domain.dispositivos.modelos import DispositivoNoAutorizado, DispositivoNoEncontrado
from bff_movil.domain.sesion.errores import ClienteNoEncontrado, ServicioNoDisponible

SUB = "sub-sintetico"
CLIENTE_ID = UUID("00000000-0000-4000-8000-000000000001")
DISPOSITIVO_ID = UUID("00000000-0000-4000-8000-0000000000d1")
TOKEN = "token-sintetico-entregado"
BASE = "http://avisos.test"
RUTA = f"{BASE}/v1/clientes/{CLIENTE_ID}/dispositivos"


class Resolutor:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.subs: list[str] = []

    async def resolver(self, sub: str) -> UUID:
        self.subs.append(sub)
        if self.error:
            raise self.error
        return CLIENTE_ID


class Dispositivos:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.registros: list[tuple[UUID, str, str]] = []
        self.bajas: list[tuple[UUID, UUID, str]] = []

    async def registrar(self, cliente_id: UUID, token: str, actor_id: str) -> UUID:
        self.registros.append((cliente_id, token, actor_id))
        if self.error:
            raise self.error
        return DISPOSITIVO_ID

    async def dar_de_baja(self, cliente_id: UUID, dispositivo_id: UUID, actor_id: str) -> None:
        self.bajas.append((cliente_id, dispositivo_id, actor_id))
        if self.error:
            raise self.error


@pytest.mark.anyio
async def test_registrar_resuelve_el_cliente_desde_el_sub() -> None:
    resolutor, gestion = Resolutor(), Dispositivos()

    resultado = await RegistrarDispositivo(resolutor, gestion).ejecutar(SUB, TOKEN)

    assert resultado == DISPOSITIVO_ID
    assert resolutor.subs == [SUB]
    assert gestion.registros == [(CLIENTE_ID, TOKEN, SUB)]


@pytest.mark.anyio
async def test_dar_de_baja_resuelve_el_cliente_desde_el_sub() -> None:
    resolutor, gestion = Resolutor(), Dispositivos()

    await DarDeBajaDispositivo(resolutor, gestion).ejecutar(SUB, DISPOSITIVO_ID)

    assert gestion.bajas == [(CLIENTE_ID, DISPOSITIVO_ID, SUB)]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error", [ClienteNoEncontrado(), ServicioNoDisponible("ERROR")], ids=["cliente", "caida"]
)
async def test_sin_cliente_no_llama_a_avisos(error: Exception) -> None:
    gestion = Dispositivos()

    with pytest.raises(type(error)):
        await RegistrarDispositivo(Resolutor(error), gestion).ejecutar(SUB, TOKEN)
    with pytest.raises(type(error)):
        await DarDeBajaDispositivo(Resolutor(error), gestion).ejecutar(SUB, DISPOSITIVO_ID)

    assert gestion.registros == [] and gestion.bajas == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error",
    [
        DispositivoNoAutorizado(),
        DispositivoNoEncontrado(),
        ServicioNoDisponible("TIEMPO_AGOTADO"),
        ServicioNoDisponible("ERROR"),
    ],
)
async def test_dar_de_baja_propaga_los_errores(error: Exception) -> None:
    with pytest.raises(type(error)):
        await DarDeBajaDispositivo(Resolutor(), Dispositivos(error)).ejecutar(SUB, DISPOSITIVO_ID)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error", [ServicioNoDisponible("TIEMPO_AGOTADO"), ServicioNoDisponible("ERROR")]
)
async def test_registrar_propaga_la_caida_de_avisos(error: Exception) -> None:
    with pytest.raises(ServicioNoDisponible):
        await RegistrarDispositivo(Resolutor(), Dispositivos(error)).ejecutar(SUB, TOKEN)


def _adaptador(
    respuesta: httpx.Response | Exception,
) -> tuple[DispositivosHttp, list[httpx.Request]]:
    vistas: list[httpx.Request] = []

    def atender(request: httpx.Request) -> httpx.Response:
        vistas.append(request)
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta

    cliente = httpx.AsyncClient(transport=httpx.MockTransport(atender))
    return DispositivosHttp(cliente, BASE, 500), vistas


@pytest.mark.anyio
async def test_adaptador_registra_con_el_token_solo_en_el_cuerpo() -> None:
    adaptador, vistas = _adaptador(httpx.Response(201, json={"dispositivoId": str(DISPOSITIVO_ID)}))

    resultado = await adaptador.registrar(CLIENTE_ID, TOKEN, SUB)

    assert resultado == DISPOSITIVO_ID
    assert str(vistas[0].url) == RUTA
    assert TOKEN not in str(vistas[0].url)
    assert json.loads(vistas[0].content) == {"token": TOKEN}
    assert vistas[0].headers["X-Actor-Id"] == SUB
    assert vistas[0].headers["X-Actor-Rol"] == "CLIENTE"


@pytest.mark.anyio
async def test_adaptador_da_de_baja_con_actor_cliente() -> None:
    adaptador, vistas = _adaptador(httpx.Response(204))

    await adaptador.dar_de_baja(CLIENTE_ID, DISPOSITIVO_ID, SUB)

    assert vistas[0].method == "DELETE"
    assert str(vistas[0].url) == f"{RUTA}/{DISPOSITIVO_ID}"
    assert vistas[0].headers["X-Actor-Rol"] == "CLIENTE"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("estado", "codigo", "esperado"),
    [
        (404, "DISPOSITIVO_NO_ENCONTRADO", DispositivoNoEncontrado),
        (403, "DISPOSITIVO_NO_AUTORIZADO", DispositivoNoAutorizado),
        (403, "ROL_NO_AUTORIZADO", ServicioNoDisponible),
        (404, None, ServicioNoDisponible),
        (500, None, ServicioNoDisponible),
    ],
)
async def test_adaptador_traduce_los_errores_de_baja(
    estado: int, codigo: str | None, esperado: type[Exception]
) -> None:
    adaptador, _ = _adaptador(httpx.Response(estado, json={"codigo": codigo} if codigo else {}))

    with pytest.raises(esperado):
        await adaptador.dar_de_baja(CLIENTE_ID, DISPOSITIVO_ID, SUB)


@pytest.mark.anyio
async def test_adaptador_trata_un_cuerpo_no_json_como_error_generico() -> None:
    adaptador, _ = _adaptador(httpx.Response(404, content=b"no es json"))

    with pytest.raises(ServicioNoDisponible):
        await adaptador.dar_de_baja(CLIENTE_ID, DISPOSITIVO_ID, SUB)


@pytest.mark.anyio
@pytest.mark.parametrize("cuerpo", [{"basura": 1}, {"dispositivoId": "no-uuid"}])
async def test_adaptador_rechaza_una_respuesta_de_registro_invalida(
    cuerpo: dict[str, object],
) -> None:
    adaptador, _ = _adaptador(httpx.Response(201, json=cuerpo))

    with pytest.raises(ServicioNoDisponible):
        await adaptador.registrar(CLIENTE_ID, TOKEN, SUB)


@pytest.mark.anyio
async def test_adaptador_falla_cerrado_ante_timeout_sin_reintentar() -> None:
    adaptador, vistas = _adaptador(httpx.ReadTimeout("lento"))

    with pytest.raises(ServicioNoDisponible) as info:
        await adaptador.registrar(CLIENTE_ID, TOKEN, SUB)

    assert info.value.motivo == "TIEMPO_AGOTADO"
    assert len(vistas) == 1


@pytest.mark.anyio
async def test_adaptador_falla_cerrado_ante_5xx_sin_filtrar_el_token() -> None:
    adaptador, vistas = _adaptador(httpx.Response(503, json={"codigo": "X"}))

    with pytest.raises(ServicioNoDisponible) as info:
        await adaptador.registrar(CLIENTE_ID, TOKEN, SUB)

    assert TOKEN not in repr(info.value) and TOKEN not in str(info.value.__cause__)
    assert len(vistas) == 1
