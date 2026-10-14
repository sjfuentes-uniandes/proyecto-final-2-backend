import asyncio
from typing import Any

import httpx
import jwt
import pytest
from bff_movil.adapters.inbound.http.jwks import ClaveNoEncontrada, JwksNoDisponible, ProveedorJwks

EMISOR = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_clientes"
TTL_S = 3600
REFRESCO_MIN_S = 60

pytestmark = pytest.mark.anyio


class Servidor:
    """Emisor simulado: cuenta las consultas y cambia las claves que publica."""

    def __init__(self, llave: Any) -> None:
        self.llave = llave
        self.kids = ["kid-1"]
        self.estado = 200
        self.cuerpo: Any = None
        self.consultas = 0
        self.tiempo = 0.0

    def manejar(self, _: httpx.Request) -> httpx.Response:
        self.consultas += 1
        if self.estado != 200:
            return httpx.Response(self.estado)
        if self.cuerpo is not None:
            return httpx.Response(200, json=self.cuerpo)
        publica = jwt.algorithms.RSAAlgorithm.to_jwk(self.llave.public_key(), as_dict=True)
        return httpx.Response(200, json={"keys": [{**publica, "kid": kid} for kid in self.kids]})


@pytest.fixture
def servidor(llave_privada: Any) -> Servidor:
    return Servidor(llave_privada)


@pytest.fixture
async def proveedor(servidor: Servidor) -> Any:
    cliente = httpx.AsyncClient(transport=httpx.MockTransport(servidor.manejar))
    yield ProveedorJwks(
        cliente,
        ttl_s=TTL_S,
        timeout_ms=500,
        refresco_min_s=REFRESCO_MIN_S,
        reloj=lambda: servidor.tiempo,
    )
    await cliente.aclose()


async def test_guarda_las_claves_dentro_del_ttl(
    proveedor: ProveedorJwks, servidor: Servidor
) -> None:
    await proveedor.obtener(EMISOR, "kid-1")
    servidor.tiempo = TTL_S - 1
    await proveedor.obtener(EMISOR, "kid-1")

    assert servidor.consultas == 1


async def test_refresca_las_claves_al_vencer_el_ttl(
    proveedor: ProveedorJwks, servidor: Servidor
) -> None:
    await proveedor.obtener(EMISOR, "kid-1")
    servidor.tiempo = TTL_S

    await proveedor.obtener(EMISOR, "kid-1")

    assert servidor.consultas == 2


async def test_un_kid_nuevo_refresca_las_claves_despues_del_intervalo_minimo(
    proveedor: ProveedorJwks, servidor: Servidor
) -> None:
    await proveedor.obtener(EMISOR, "kid-1")
    servidor.kids = ["kid-1", "kid-2"]
    servidor.tiempo = REFRESCO_MIN_S

    await proveedor.obtener(EMISOR, "kid-2")

    assert servidor.consultas == 2


async def test_un_kid_inventado_no_fuerza_una_consulta_por_solicitud(
    proveedor: ProveedorJwks, servidor: Servidor
) -> None:
    await proveedor.obtener(EMISOR, "kid-1")
    servidor.tiempo = REFRESCO_MIN_S - 1

    for _ in range(3):
        with pytest.raises(ClaveNoEncontrada):
            await proveedor.obtener(EMISOR, "kid-inventado")

    assert servidor.consultas == 1


async def test_sin_copia_guardada_un_emisor_caido_no_esta_disponible(
    proveedor: ProveedorJwks, servidor: Servidor
) -> None:
    servidor.estado = 500

    with pytest.raises(JwksNoDisponible):
        await proveedor.obtener(EMISOR, "kid-1")


@pytest.mark.parametrize(
    "cuerpo", [{"sin": "keys"}, {"keys": "no-es-lista"}, {"keys": [{"kid": "kid-1"}]}, []]
)
async def test_un_jwks_invalido_no_esta_disponible(
    proveedor: ProveedorJwks, servidor: Servidor, cuerpo: Any
) -> None:
    servidor.cuerpo = cuerpo

    with pytest.raises(JwksNoDisponible):
        await proveedor.obtener(EMISOR, "kid-1")


async def test_con_copia_guardada_conserva_las_claves_si_el_emisor_cae(
    proveedor: ProveedorJwks, servidor: Servidor
) -> None:
    await proveedor.obtener(EMISOR, "kid-1")
    servidor.estado = 500
    servidor.tiempo = TTL_S

    assert await proveedor.obtener(EMISOR, "kid-1")
    assert await proveedor.obtener(EMISOR, "kid-1")

    assert servidor.consultas == 2


async def test_solicitudes_concurrentes_consultan_al_emisor_una_vez(
    proveedor: ProveedorJwks, servidor: Servidor
) -> None:
    await asyncio.gather(*(proveedor.obtener(EMISOR, "kid-1") for _ in range(10)))

    assert servidor.consultas == 1
