"""Auditoría de accesos denegados (DS3-09)."""

from datetime import datetime

import pytest
from siniestros.application.siniestro.acceso import RegistrarAccesoDenegado
from siniestros.application.siniestro.vistas import (
    AccesoDenegado,
    MotivoDenegacion,
    OrigenDenegacion,
    RecursoSiniestro,
)

pytestmark = pytest.mark.anyio


async def test_escribe_el_evento_en_el_outbox(repo, reloj, ahora: datetime) -> None:
    acceso = AccesoDenegado(
        "a0a0",
        None,
        RecursoSiniestro.BANDEJA,
        None,
        MotivoDenegacion.SIN_GRUPO,
        OrigenDenegacion.BFF_WEB,
    )
    await RegistrarAccesoDenegado(repo, reloj).ejecutar(acceso)
    assert repo.eventos == [("AccesoSiniestroDenegado", acceso)]
    assert repo.transacciones == 1
