from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from siniestros.application.siniestro.borradores import Borradores, EvidenciaSolicitada
from siniestros.application.siniestro.envio import clave_permanente
from siniestros.application.siniestro.errores import (
    BorradorNoEncontrado,
    DependenciaNoDisponible,
    DuracionVideoExcedida,
    EvidenciaNoCargada,
    EvidenciaYaConfirmada,
    IntegridadEvidenciaInvalida,
    TamanoEvidenciaExcedido,
    TipoEvidenciaNoAdmitido,
)
from siniestros.config import Settings
from siniestros.domain.siniestro.modelos import IntegridadEvidencia, TipoEvidencia

pytestmark = pytest.mark.anyio

BORRADOR_ID = UUID("00000000-0000-4000-8000-000000000001")
POLIZA_ID = UUID("00000000-0000-4000-8000-000000000002")
CLAVE = UUID("00000000-0000-4000-8000-000000000003")
AHORA = datetime(2026, 1, 1, tzinfo=UTC)
SHA256 = "a" * 64


class Repositorio:
    def __init__(self) -> None:
        self.borrador: tuple[Any, ...] | None = None
        self.evidencia: tuple[Any, ...] | None = None
        self.borradores: list[dict[str, Any]] = []
        self.detalle: dict[str, Any] | None = None
        self.carga: dict[str, Any] | None = None
        self.confirmaciones: list[tuple[Any, ...]] = []

    async def listar_borradores(self, actor_id: str) -> list[dict[str, Any]]:
        return self.borradores

    async def consultar_borrador(self, actor_id: str, borrador_id: UUID) -> dict[str, Any] | None:
        return self.detalle

    async def crear_borrador(self, *argumentos: Any) -> UUID:
        self.borrador = argumentos
        return argumentos[2]

    async def agregar_evidencia_borrador(self, *argumentos: Any) -> None:
        self.evidencia = argumentos

    async def evidencia_borrador(
        self, actor_id: str, borrador_id: UUID, evidencia_id: UUID
    ) -> dict[str, Any] | None:
        return self.carga

    async def confirmar_evidencia_borrador(self, *argumentos: Any) -> datetime | None:
        self.confirmaciones.append(argumentos)
        if self.carga is not None:
            self.carga["estadoCarga"] = "CARGADA"
            self.carga["cargaConfirmadaEn"] = self.carga["cargaConfirmadaEn"] or argumentos[3]
            self.carga["claveObjeto"] = argumentos[4]
            return self.carga["cargaConfirmadaEn"]
        return None


class Almacen:
    def __init__(self) -> None:
        self.solicitudes: list[tuple[Any, ...]] = []
        self.integridad = IntegridadEvidencia.VERIFICADA
        self.verificaciones: list[tuple[str, str]] = []

    async def firmar_carga(self, *argumentos: Any) -> tuple[str, dict[str, str]]:
        self.solicitudes.append(argumentos)
        return "https://s3.test/upload", {"key": argumentos[0]}

    async def verificar(self, clave: str, sha256: str) -> IntegridadEvidencia:
        self.verificaciones.append((clave, sha256))
        return self.integridad

    async def conservar(
        self,
        origen: str,
        destino: str,
        sha256: str,
        tamano_bytes: int,
        tipo_contenido: str,
        cifrado: str,
    ) -> None:
        integridad = await self.verificar(origen, sha256)
        if integridad is IntegridadEvidencia.NO_DISPONIBLE:
            raise EvidenciaNoCargada
        if integridad is IntegridadEvidencia.ALTERADA:
            raise IntegridadEvidenciaInvalida
        if integridad is IntegridadEvidencia.NO_VERIFICABLE:
            raise DependenciaNoDisponible("evidencias")


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


async def test_crear_guarda_descripcion_y_la_incluye_en_la_huella_de_idempotencia() -> None:
    casos, repo, _ = crear_borradores()
    cliente_id = UUID("00000000-0000-4000-8000-000000000004")
    copia_poliza = {"numero": "900-2026-000001"}

    await casos.crear(
        cliente_id,
        POLIZA_ID,
        "El vehículo recibió un impacto.",
        copia_poliza,
        CLAVE,
        "actor",
        "corr",
    )
    assert repo.borrador is not None
    huella_primera, descripcion = repo.borrador[1], repo.borrador[6]

    await casos.crear(
        cliente_id,
        POLIZA_ID,
        "El vehículo tuvo daños en la puerta.",
        copia_poliza,
        CLAVE,
        "actor",
        "corr",
    )

    assert descripcion == "El vehículo recibió un impacto."
    assert repo.borrador is not None
    assert repo.borrador[1] != huella_primera


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


async def test_lista_y_consulta_borradores_con_estado_y_evidencias() -> None:
    casos, repo, _ = crear_borradores()
    repo.borradores = [{"borradorId": BORRADOR_ID, "estado": "ABIERTO"}]
    repo.detalle = {"borradorId": BORRADOR_ID, "evidencias": [], "estado": "ABIERTO"}

    listado = await casos.listar("actor")
    detalle = await casos.consultar("actor", BORRADOR_ID)

    assert listado == {"items": [{"borradorId": BORRADOR_ID, "estado": "ABIERTO"}]}
    assert detalle == {"borradorId": BORRADOR_ID, "evidencias": [], "estado": "ABIERTO"}


async def test_consultar_no_revela_un_borrador_ajeno_o_inexistente() -> None:
    casos, _, _ = crear_borradores()

    with pytest.raises(BorradorNoEncontrado):
        await casos.consultar("otro-actor", BORRADOR_ID)


async def test_confirmar_verifica_s3_y_es_idempotente() -> None:
    casos, repo, almacen = crear_borradores()
    repo.carga = {
        "estadoCarga": "PENDIENTE",
        "cargaConfirmadaEn": None,
        "claveObjeto": f"tmp/{BORRADOR_ID}/evidencia",
        "sha256": SHA256,
        "tipoContenido": "image/jpeg",
        "tamanoBytes": 123,
    }

    primera = await casos.confirmar_evidencia("actor", BORRADOR_ID, CLAVE)
    segunda = await casos.confirmar_evidencia("actor", BORRADOR_ID, CLAVE)

    assert (
        primera
        == segunda
        == {
            "evidenciaId": CLAVE,
            "estadoCarga": "CARGADA",
            "cargaConfirmadaEn": AHORA,
        }
    )
    assert almacen.verificaciones == [
        (f"tmp/{BORRADOR_ID}/evidencia", SHA256),
        (clave_permanente(BORRADOR_ID, CLAVE), SHA256),
    ]
    assert len(repo.confirmaciones) == 2


@pytest.mark.parametrize(
    ("integridad", "error"),
    [
        (IntegridadEvidencia.NO_DISPONIBLE, EvidenciaNoCargada),
        (IntegridadEvidencia.ALTERADA, IntegridadEvidenciaInvalida),
        (IntegridadEvidencia.NO_VERIFICABLE, DependenciaNoDisponible),
    ],
)
async def test_confirmar_rechaza_objeto_ausente_alterado_o_no_verificable(
    integridad: IntegridadEvidencia, error: type[Exception]
) -> None:
    casos, repo, almacen = crear_borradores()
    repo.carga = {
        "estadoCarga": "PENDIENTE",
        "cargaConfirmadaEn": None,
        "claveObjeto": f"tmp/{BORRADOR_ID}/evidencia",
        "sha256": SHA256,
        "tipoContenido": "image/jpeg",
        "tamanoBytes": 123,
    }
    almacen.integridad = integridad

    with pytest.raises(error):
        await casos.confirmar_evidencia("actor", BORRADOR_ID, CLAVE)

    assert repo.confirmaciones == []


async def test_confirmar_evidencia_ajena_falla_sin_consultar_s3() -> None:
    casos, repo, almacen = crear_borradores()
    repo.carga = None

    with pytest.raises(BorradorNoEncontrado):
        await casos.confirmar_evidencia("otro-actor", BORRADOR_ID, CLAVE)

    assert almacen.verificaciones == []


async def test_renovar_url_reutiliza_la_evidencia_pendiente() -> None:
    casos, repo, almacen = crear_borradores()
    repo.carga = {
        "estadoCarga": "PENDIENTE",
        "cargaConfirmadaEn": None,
        "claveObjeto": f"tmp/{BORRADOR_ID}/{CLAVE}",
        "tipoContenido": "image/jpeg",
        "tamanoBytes": 123,
        "sha256": SHA256,
    }

    resultado = await casos.renovar_carga("actor", BORRADOR_ID, CLAVE)

    assert resultado["evidenciaId"] == CLAVE
    assert resultado["estadoCarga"] == "PENDIENTE"
    assert resultado["carga"]["url"] == "https://s3.test/upload"
    assert almacen.solicitudes == [
        (
            f"tmp/{BORRADOR_ID}/{CLAVE}",
            "image/jpeg",
            123,
            SHA256,
            300,
            "AES256",
        )
    ]
    assert repo.evidencia is None


async def test_no_renueva_url_de_evidencia_ya_confirmada() -> None:
    casos, repo, almacen = crear_borradores()
    repo.carga = {
        "estadoCarga": "CARGADA",
        "cargaConfirmadaEn": AHORA,
        "claveObjeto": f"tmp/{BORRADOR_ID}/{CLAVE}",
        "tipoContenido": "image/jpeg",
        "tamanoBytes": 123,
        "sha256": SHA256,
    }

    with pytest.raises(EvidenciaYaConfirmada):
        await casos.renovar_carga("actor", BORRADOR_ID, CLAVE)

    assert almacen.solicitudes == []
