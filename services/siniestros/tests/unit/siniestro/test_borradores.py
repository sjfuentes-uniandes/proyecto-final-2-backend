from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from siniestros.application.siniestro.borradores import Borradores, EvidenciaSolicitada
from siniestros.application.siniestro.errores import (
    DuracionVideoExcedida,
    TamanoEvidenciaExcedido,
    TipoEvidenciaNoAdmitido,
)
from siniestros.config import Settings
from siniestros.domain.siniestro.modelos import TipoEvidencia

pytestmark = pytest.mark.anyio

BORRADOR_ID = UUID("00000000-0000-4000-8000-000000000001")
POLIZA_ID = UUID("00000000-0000-4000-8000-000000000002")
AHORA = datetime(2026, 1, 1, tzinfo=UTC)
SHA256 = "a" * 64


class Repositorio:
    def __init__(self) -> None:
        self.borrador: tuple[Any, ...] | None = None
        self.evidencia: tuple[Any, ...] | None = None

    async def crear_borrador(self, *argumentos: Any) -> UUID:
        self.borrador = argumentos
        return argumentos[2]

    async def agregar_evidencia_borrador(self, *argumentos: Any) -> None:
        self.evidencia = argumentos


class Almacen:
    def __init__(self) -> None:
        self.solicitudes: list[tuple[Any, ...]] = []

    async def firmar_carga(self, *argumentos: Any) -> tuple[str, dict[str, str]]:
        self.solicitudes.append(argumentos)
        return "https://s3.test/upload", {"key": argumentos[0]}


class Reloj:
    def ahora(self) -> datetime:
        return AHORA


def crear_borradores() -> tuple[Borradores, Repositorio, Almacen]:
    repo, almacen = Repositorio(), Almacen()
    return Borradores(repo, almacen, Reloj(), Settings()), repo, almacen


def solicitud_foto(**cambios: Any) -> EvidenciaSolicitada:
    datos: dict[str, Any] = {
        "nombre_archivo": "foto.jpg",
        "tipo": TipoEvidencia.FOTOGRAFIA,
        "tipo_contenido": "image/jpeg",
        "tamano_bytes": Settings().evidencia_foto_max_bytes,
        "sha256": SHA256,
        "capturada_en": AHORA,
        "duracion_segundos": None,
    }
    return EvidenciaSolicitada(**{**datos, **cambios})


async def test_limites_publica_la_configuracion_de_evidencias() -> None:
    casos, _, _ = crear_borradores()

    assert casos.limites() == {
        "maxEvidencias": 10,
        "formatosFoto": ["image/jpeg", "image/png"],
        "formatosVideo": ["video/mp4"],
        "tamanoMaximoFotoBytes": 10 * 1024 * 1024,
        "tamanoMaximoVideoBytes": 50 * 1024 * 1024,
        "duracionMaximaVideoSegundos": 60,
        "urlCargaExpiraSegundos": 300,
    }


async def test_acepta_el_tamano_maximo_y_firma_carga_con_hash_y_ttl() -> None:
    casos, repo, almacen = crear_borradores()

    resultado = await casos.agregar_evidencia("actor", BORRADOR_ID, solicitud_foto())

    assert len(almacen.solicitudes) == 1
    clave, tipo, tamano, sha256, ttl, cifrado = almacen.solicitudes[0]
    assert clave.startswith(f"tmp/{BORRADOR_ID}/")
    assert (tipo, tamano, sha256, ttl, cifrado) == (
        "image/jpeg",
        10 * 1024 * 1024,
        SHA256,
        300,
        "AES256",
    )
    assert resultado["carga"]["metodo"] == "POST"
    assert resultado["carga"]["url"] == "https://s3.test/upload"
    assert repo.evidencia is not None
    assert repo.evidencia[0:2] == ("actor", BORRADOR_ID)


@pytest.mark.parametrize(
    ("cambios", "error"),
    [
        ({"tipo_contenido": "application/pdf"}, TipoEvidenciaNoAdmitido),
        ({"tamano_bytes": 10 * 1024 * 1024 + 1}, TamanoEvidenciaExcedido),
    ],
)
async def test_rechaza_formato_o_tamano_fuera_de_limites(
    cambios: dict[str, Any], error: type[Exception]
) -> None:
    casos, repo, almacen = crear_borradores()

    with pytest.raises(error):
        await casos.agregar_evidencia("actor", BORRADOR_ID, solicitud_foto(**cambios))

    assert almacen.solicitudes == []
    assert repo.evidencia is None


async def test_rechaza_video_mas_largo_que_el_limite() -> None:
    casos, repo, almacen = crear_borradores()
    video = solicitud_foto(
        nombre_archivo="video.mp4",
        tipo=TipoEvidencia.VIDEO,
        tipo_contenido="video/mp4",
        tamano_bytes=50 * 1024 * 1024,
        duracion_segundos=61,
    )

    with pytest.raises(DuracionVideoExcedida):
        await casos.agregar_evidencia("actor", BORRADOR_ID, video)

    assert almacen.solicitudes == []
    assert repo.evidencia is None
