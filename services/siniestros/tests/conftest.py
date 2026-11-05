"""Fábricas de las pruebas de siniestros (HU-W19). Solo datos sintéticos.

El reloj es siempre un argumento: ninguna prueba depende de la hora real. `AHORA` es el instante de
referencia y los datos se definen relativos a él.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from siniestros.domain.siniestro.modelos import (
    Amparo,
    Beneficiario,
    CopiaPoliza,
    EntradaHistorial,
    EstadoSiniestro,
    Evidencia,
    OrigenSiniestro,
    Siniestro,
    TipoEvidencia,
    TipoSoporte,
    Ubicacion,
    Vigencia,
)

AHORA = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)
ANALISTA = UUID("a0a0a0a0-0000-4000-8000-000000000001")
SHA = "a" * 64
BOGOTA = Ubicacion(descripcion="Bogotá D.C.", lat=4.652, lon=-74.0555)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def ahora() -> datetime:
    return AHORA


@pytest.fixture
def ingenuo() -> datetime:
    """Un instante sin zona horaria: el dominio debe rechazarlo."""
    return AHORA.replace(tzinfo=None)


@pytest.fixture
def copia() -> Callable[..., CopiaPoliza]:
    """Póliza de dispositivos vigente desde hace 180 días, con 30 de carencia y 30 de aviso."""

    def fabricar(**cambios: Any) -> CopiaPoliza:
        datos: dict[str, Any] = {
            "numero": "220-2026-011294",
            "ramo": "Dispositivos",
            "moneda": "COP",
            "amparos": (Amparo("DANO_ACCIDENTAL", "Daño accidental", "3000000.00"),),
            "vigencia": Vigencia(date(2026, 5, 9), date(2027, 5, 9)),
            "carencia_dias": 30,
            "plazo_aviso_dias": 30,
            "primas_al_dia": True,
            "beneficiario": Beneficiario("ben-sint-004", "****1004"),
        }
        datos.update(cambios)
        return CopiaPoliza(**datos)

    return fabricar


@pytest.fixture
def siniestro(copia: Callable[..., CopiaPoliza]) -> Callable[..., Siniestro]:
    """Un REPORTADO hace 14 h, con el evento 41 min antes del reporte."""

    def fabricar(**cambios: Any) -> Siniestro:
        reportado = AHORA - timedelta(hours=14)
        datos: dict[str, Any] = {
            "id": uuid4(),
            "codigo": "88-2026-0442",
            "origen": OrigenSiniestro.MOVIL,
            "estado": EstadoSiniestro.REPORTADO,
            "poliza_id": uuid4(),
            "cliente_id": uuid4(),
            "copia_poliza": copia(),
            "causa": "DANO_ACCIDENTAL",
            "fecha_evento": reportado - timedelta(minutes=41),
            "ubicacion_declarada": BOGOTA,
            "reportado_en": reportado,
            "correlation_id": "corr-w19",
        }
        datos.update(cambios)
        return Siniestro(**datos)

    return fabricar


@pytest.fixture
def evidencia() -> Callable[..., Evidencia]:
    def fabricar(**cambios: Any) -> Evidencia:
        evidencia_id = cambios.pop("id", uuid4())
        siniestro_id = cambios.pop("siniestro_id", uuid4())
        datos: dict[str, Any] = {
            "id": evidencia_id,
            "siniestro_id": siniestro_id,
            "nombre_archivo": "evidencia_dano_01.jpg",
            "tipo": TipoEvidencia.FOTOGRAFIA,
            "clave_objeto": f"siniestros/{siniestro_id}/{evidencia_id}",
            "sha256": SHA,
            "capturada_en": AHORA - timedelta(hours=13, minutes=53),
            "ubicacion": Ubicacion(lat=4.6512, lon=-74.056),
        }
        datos.update(cambios)
        return Evidencia(**datos)

    return fabricar


@pytest.fixture
def soporte(evidencia: Callable[..., Evidencia]) -> Callable[[TipoSoporte], Evidencia]:
    """Un archivo de soporte (factura, cédula, …) escaneado, sin coordenadas."""

    def fabricar(tipo: TipoSoporte) -> Evidencia:
        return evidencia(
            nombre_archivo=f"{tipo.value.lower()}.pdf",
            tipo=TipoEvidencia.SOPORTE,
            soporte_tipo=tipo,
            ubicacion=Ubicacion(descripcion="Escaneado en el sitio"),
        )

    return fabricar


@pytest.fixture
def entrada() -> Callable[..., EntradaHistorial]:
    def fabricar(accion: str, ocurrido_en: datetime, **cambios: Any) -> EntradaHistorial:
        datos: dict[str, Any] = {
            "id": uuid4(),
            "siniestro_id": uuid4(),
            "accion": accion,
            "ocurrido_en": ocurrido_en,
        }
        datos.update(cambios)
        return EntradaHistorial(**datos)

    return fabricar
