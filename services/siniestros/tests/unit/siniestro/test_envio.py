from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from siniestros.application.siniestro.envio import (
    BorradorEnvio,
    EnviarBorrador,
    EnviosRepositorio,
    PolizaReporte,
    clave_permanente,
    validar_reporte,
)
from siniestros.application.siniestro.errores import (
    DependenciaNoDisponible,
    EvidenciaNoCargada,
    PolizaNoVigente,
    SolicitudInvalida,
)
from siniestros.application.siniestro.puertos import AlmacenEvidencias, Reloj
from siniestros.config import Settings
from siniestros.domain.siniestro.modelos import Vigencia

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("otra_poliza", [False, True])
def test_rechaza_poliza_ausente_o_distinta(
    borrador_envio: BorradorEnvio,
    poliza_reporte: PolizaReporte,
    ahora: datetime,
    otra_poliza: bool,
) -> None:
    poliza = replace(poliza_reporte, id=uuid4()) if otra_poliza else None

    with pytest.raises(SolicitudInvalida) as error:
        validar_reporte(borrador_envio, poliza, ahora, Settings())

    assert error.value.detalles == {"campos": ["copiaPoliza"]}


@pytest.mark.parametrize(("estado", "vigente"), [("EMITIDA", True), ("ACTIVA", False)])
def test_rechaza_poliza_no_activa_o_no_vigente(
    borrador_envio: BorradorEnvio,
    poliza_reporte: PolizaReporte,
    ahora: datetime,
    estado: str,
    vigente: bool,
) -> None:
    with pytest.raises(PolizaNoVigente):
        validar_reporte(
            borrador_envio,
            replace(poliza_reporte, estado=estado, vigente=vigente),
            ahora,
            Settings(),
        )


@pytest.mark.parametrize(
    "vigencia",
    [
        None,
        Vigencia(date(2025, 1, 1), date(2025, 12, 31)),
        Vigencia(date(2027, 1, 1), date(2027, 12, 31)),
    ],
)
def test_rechaza_poliza_sin_fechas_o_fuera_de_vigencia(
    borrador_envio: BorradorEnvio,
    poliza_reporte: PolizaReporte,
    ahora: datetime,
    vigencia: Vigencia | None,
) -> None:
    poliza = replace(poliza_reporte, copia=replace(poliza_reporte.copia, vigencia=vigencia))

    with pytest.raises(PolizaNoVigente):
        validar_reporte(borrador_envio, poliza, ahora, Settings())


@pytest.mark.parametrize(
    ("cambios", "campos"),
    [
        ({"descripcion": " \n "}, ["descripcion"]),
        ({"causa": None}, ["causa"]),
        ({"causa": "AMPARO_AJENO"}, ["causa"]),
        ({"fecha_evento": None}, ["fechaEvento"]),
        ({"fecha_evento": datetime.fromisoformat("2025-01-01T10:00:00-05:00")}, ["fechaEvento"]),
        ({"evidencias": ()}, ["evidencias"]),
        (
            {"descripcion": "", "causa": None, "fecha_evento": None, "evidencias": ()},
            ["descripcion", "causa", "fechaEvento", "evidencias"],
        ),
    ],
)
def test_informa_los_campos_invalidos_del_reporte(
    borrador_envio: BorradorEnvio,
    poliza_reporte: PolizaReporte,
    ahora: datetime,
    cambios: dict[str, Any],
    campos: list[str],
) -> None:
    with pytest.raises(SolicitudInvalida) as error:
        validar_reporte(replace(borrador_envio, **cambios), poliza_reporte, ahora, Settings())

    assert error.value.detalles == {"campos": campos}


@pytest.mark.parametrize("sin_zona", [False, True])
def test_no_admite_evento_futuro_o_sin_zona(
    borrador_envio: BorradorEnvio,
    poliza_reporte: PolizaReporte,
    ahora: datetime,
    sin_zona: bool,
) -> None:
    fecha = ahora.replace(tzinfo=None) if sin_zona else ahora + timedelta(seconds=1)
    with pytest.raises(SolicitudInvalida) as error:
        validar_reporte(
            replace(borrador_envio, fecha_evento=fecha),
            poliza_reporte,
            ahora,
            Settings(),
        )

    assert error.value.detalles == {"campos": ["fechaEvento"]}


def test_identifica_las_evidencias_pendientes_sin_incluir_las_confirmadas(
    borrador_envio: BorradorEnvio, poliza_reporte: PolizaReporte, ahora: datetime
) -> None:
    pendiente = replace(borrador_envio.evidencias[0], id=uuid4(), estado_carga="PENDIENTE")
    borrador = replace(borrador_envio, evidencias=(*borrador_envio.evidencias, pendiente))

    with pytest.raises(EvidenciaNoCargada) as error:
        validar_reporte(borrador, poliza_reporte, ahora, Settings())

    assert error.value.detalles == {"evidencias": [str(pendiente.id)]}


@pytest.mark.parametrize("confirmado", [False, True])
async def test_envia_los_archivos_antes_de_convertir_y_no_los_repite_al_reintentar(
    borrador_envio: BorradorEnvio,
    poliza_reporte: PolizaReporte,
    ahora: datetime,
    confirmado: bool,
) -> None:
    resultado = {"siniestroId": str(uuid4()), "estado": "REPORTADO"}
    borrador = replace(borrador_envio, resultado=resultado if confirmado else None)
    repo = AsyncMock(spec=EnviosRepositorio)
    repo.preparar.return_value = borrador
    repo.convertir.return_value = resultado
    almacen = AsyncMock(spec=AlmacenEvidencias)
    reloj = Mock(spec=Reloj)
    reloj.ahora.return_value = ahora
    poliza = None if confirmado else poliza_reporte
    clave = uuid4()

    async def convertir(*_: Any) -> dict[str, Any]:
        if not confirmado:
            almacen.conservar.assert_awaited_once()
        return resultado

    repo.convertir.side_effect = convertir
    envio = EnviarBorrador(repo, almacen, reloj, "AES256", Settings())

    assert await envio.ejecutar(borrador.actor_id, borrador.id, clave, poliza) == resultado

    repo.preparar.assert_awaited_once_with(borrador.actor_id, borrador.id, clave)
    repo.convertir.assert_awaited_once_with(borrador, clave, poliza, ahora)
    if confirmado:
        almacen.conservar.assert_not_awaited()
    else:
        foto = borrador.evidencias[0]
        almacen.conservar.assert_awaited_once_with(
            foto.clave_objeto,
            clave_permanente(borrador.id, foto.id),
            foto.sha256,
            foto.tamano_bytes,
            foto.tipo_contenido,
            "AES256",
        )


async def test_no_convierte_si_falla_el_almacenamiento(
    borrador_envio: BorradorEnvio, poliza_reporte: PolizaReporte, ahora: datetime
) -> None:
    repo = AsyncMock(spec=EnviosRepositorio)
    repo.preparar.return_value = borrador_envio
    almacen = AsyncMock(spec=AlmacenEvidencias)
    almacen.conservar.side_effect = DependenciaNoDisponible("evidencias")
    reloj = Mock(spec=Reloj)
    reloj.ahora.return_value = ahora
    envio = EnviarBorrador(repo, almacen, reloj, "AES256", Settings())

    with pytest.raises(DependenciaNoDisponible):
        await envio.ejecutar(borrador_envio.actor_id, borrador_envio.id, uuid4(), poliza_reporte)

    repo.convertir.assert_not_awaited()
