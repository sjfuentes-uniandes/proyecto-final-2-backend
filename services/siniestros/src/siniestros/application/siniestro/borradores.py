"""Creación de borradores móviles y emisión de cargas directas a S3 (HU-M01)."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

from siniestros.application.siniestro.errores import (
    DuracionVideoExcedida,
    TamanoEvidenciaExcedido,
    TipoEvidenciaNoAdmitido,
)
from siniestros.application.siniestro.puertos import AlmacenEvidencias, Reloj
from siniestros.config import Settings
from siniestros.domain.siniestro.modelos import TipoEvidencia


class BorradoresRepositorio(Protocol):
    async def crear_borrador(
        self,
        clave: str,
        huella: str,
        borrador_id: UUID,
        actor_id: str,
        cliente_id: UUID,
        poliza_id: UUID,
        descripcion: str,
        copia_poliza: dict[str, Any],
        creado_en: datetime,
        correlation_id: str,
    ) -> UUID: ...

    async def agregar_evidencia_borrador(
        self,
        actor_id: str,
        borrador_id: UUID,
        evidencia_id: UUID,
        nombre_archivo: str,
        tipo: TipoEvidencia,
        clave_objeto: str,
        sha256: str,
        capturada_en: datetime | None,
        tamano_bytes: int,
        tipo_contenido: str,
        duracion_segundos: int | None,
        maximo: int,
    ) -> None: ...


@dataclass(frozen=True)
class EvidenciaSolicitada:
    nombre_archivo: str
    tipo: TipoEvidencia
    tipo_contenido: str
    tamano_bytes: int
    sha256: str
    capturada_en: datetime | None
    duracion_segundos: int | None


def _huella(
    actor_id: str,
    cliente_id: UUID,
    poliza_id: UUID,
    descripcion: str,
    copia_poliza: dict[str, Any],
) -> str:
    solicitud = json.dumps(
        {
            "actorId": actor_id,
            "clienteId": str(cliente_id),
            "polizaId": str(poliza_id),
            "descripcion": descripcion,
            "copiaPoliza": copia_poliza,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(solicitud.encode()).hexdigest()


class Borradores:
    def __init__(
        self,
        repo: BorradoresRepositorio,
        almacen: AlmacenEvidencias,
        reloj: Reloj,
        settings: Settings,
    ) -> None:
        self._repo = repo
        self._almacen = almacen
        self._reloj = reloj
        self._settings = settings

    def limites(self) -> dict[str, Any]:
        return {
            "maxEvidencias": self._settings.evidencias_max_por_borrador,
            "formatosFoto": list(self._settings.evidencia_formatos_foto),
            "formatosVideo": list(self._settings.evidencia_formatos_video),
            "tamanoMaximoFotoBytes": self._settings.evidencia_foto_max_bytes,
            "tamanoMaximoVideoBytes": self._settings.evidencia_video_max_bytes,
            "duracionMaximaVideoSegundos": self._settings.evidencia_video_max_duracion_s,
            "urlCargaExpiraSegundos": self._settings.evidencia_url_carga_ttl_s,
        }

    async def crear(
        self,
        cliente_id: UUID,
        poliza_id: UUID,
        descripcion: str,
        copia_poliza: dict[str, Any],
        clave: UUID,
        actor_id: str,
        correlation_id: str,
    ) -> UUID:
        borrador_id = uuid4()
        return await self._repo.crear_borrador(
            f"borrador:{clave}",
            _huella(actor_id, cliente_id, poliza_id, descripcion, copia_poliza),
            borrador_id,
            actor_id,
            cliente_id,
            poliza_id,
            descripcion,
            copia_poliza,
            self._reloj.ahora(),
            correlation_id,
        )

    async def agregar_evidencia(
        self,
        actor_id: str,
        borrador_id: UUID,
        solicitud: EvidenciaSolicitada,
    ) -> dict[str, Any]:
        if solicitud.tipo is TipoEvidencia.FOTOGRAFIA:
            admitidos = self._settings.evidencia_formatos_foto
            limite_bytes = self._settings.evidencia_foto_max_bytes
        else:
            admitidos = self._settings.evidencia_formatos_video
            limite_bytes = self._settings.evidencia_video_max_bytes
        if solicitud.tipo_contenido not in admitidos:
            raise TipoEvidenciaNoAdmitido(admitidos)
        if solicitud.tamano_bytes > limite_bytes:
            raise TamanoEvidenciaExcedido(limite_bytes)
        if solicitud.tipo is TipoEvidencia.VIDEO and (
            solicitud.duracion_segundos is None
            or solicitud.duracion_segundos > self._settings.evidencia_video_max_duracion_s
        ):
            raise DuracionVideoExcedida(self._settings.evidencia_video_max_duracion_s)

        evidencia_id = uuid4()
        clave_objeto = f"tmp/{borrador_id}/{evidencia_id}"
        url, campos = await self._almacen.firmar_carga(
            clave_objeto,
            solicitud.tipo_contenido,
            solicitud.tamano_bytes,
            solicitud.sha256,
            self._settings.evidencia_url_carga_ttl_s,
            self._settings.evidencias_bucket_sse,
        )
        ahora = self._reloj.ahora()
        await self._repo.agregar_evidencia_borrador(
            actor_id,
            borrador_id,
            evidencia_id,
            solicitud.nombre_archivo,
            solicitud.tipo,
            clave_objeto,
            solicitud.sha256,
            solicitud.capturada_en,
            solicitud.tamano_bytes,
            solicitud.tipo_contenido,
            solicitud.duracion_segundos,
            self._settings.evidencias_max_por_borrador,
        )
        return {
            "evidenciaId": evidencia_id,
            "nombreArchivo": solicitud.nombre_archivo,
            "tipo": solicitud.tipo,
            "tipoContenido": solicitud.tipo_contenido,
            "tamanoBytes": solicitud.tamano_bytes,
            "sha256": solicitud.sha256,
            "capturadaEn": solicitud.capturada_en,
            "duracionSegundos": solicitud.duracion_segundos,
            "carga": {
                "metodo": "POST",
                "url": url,
                "campos": campos,
                "expiraEn": ahora + timedelta(seconds=self._settings.evidencia_url_carga_ttl_s),
            },
        }
