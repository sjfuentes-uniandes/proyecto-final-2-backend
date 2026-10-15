"""RecalcularPerfil (UT-W12-20 a 22): perfil nuevo enlazado con el anterior (DT-25 y DT-26)."""

from typing import Any
from uuid import UUID, uuid4

import pytest
from cotizacion.application.perfilamiento.recalcular_perfil import RecalcularPerfil
from cotizacion.domain.perfilamiento.errores import PerfilNoEncontrado
from cotizacion.domain.perfilamiento.modelos import EstadoFuente, EstadoPerfil, Fuente

pytestmark = pytest.mark.anyio

CLAVE = UUID("00000000-0000-4000-b000-0000000000d1")


class CrearPerfilQueRegistra:
    def __init__(self, respuesta: Any) -> None:
        self.respuesta = respuesta
        self.comandos: list[Any] = []

    async def ejecutar(self, comando: Any) -> Any:
        self.comandos.append(comando)
        return self.respuesta


async def test_ut_w12_20_comando_con_el_perfil_anterior(escenario: Any, perfil_c01: Any) -> None:
    escenario.repo.perfiles[perfil_c01.id] = perfil_c01
    crear = CrearPerfilQueRegistra((perfil_c01, True))
    resultado = await RecalcularPerfil(crear, escenario.repo).ejecutar(  # type: ignore[arg-type]
        perfil_c01.id, CLAVE, "asesor-2", "corr-recalculo"
    )
    assert resultado == (perfil_c01, True)
    (cmd,) = crear.comandos
    assert (cmd.cliente_id, cmd.producto_id, cmd.datos_declarados) == (
        perfil_c01.cliente_id,
        perfil_c01.producto_id,
        perfil_c01.datos_declarados,
    )
    assert cmd.perfil_anterior_id == perfil_c01.id
    assert cmd.operacion == "perfilamiento.recalculo"
    assert (cmd.idempotency_key, cmd.actor_id, cmd.correlation_id) == (
        CLAVE,
        "asesor-2",
        "corr-recalculo",
    )


async def test_ut_w12_21_perfil_inexistente(escenario: Any) -> None:
    crear = CrearPerfilQueRegistra(None)
    with pytest.raises(PerfilNoEncontrado):
        await RecalcularPerfil(crear, escenario.repo).ejecutar(  # type: ignore[arg-type]
            uuid4(), CLAVE, "asesor", "corr"
        )
    assert crear.comandos == []


async def test_ut_w12_22_misma_clave_es_idempotente(
    escenario: Any, nuevo_comando: Any, telemetria: Any
) -> None:
    # Perfil degradado: Datos Abiertos agotó su tiempo; en el recálculo ya responde.
    escenario.ejecutor.cambios = {
        Fuente.DA_REGISTROS_PUBLICOS: {"estado": EstadoFuente.TIEMPO_AGOTADO, "campos": {}}
    }
    crear = escenario.caso()
    anterior, _ = await crear.ejecutar(nuevo_comando())
    assert anterior.resultado.estado is EstadoPerfil.DEGRADADO
    escenario.ejecutor.cambios = {}

    recalcular = RecalcularPerfil(crear, escenario.repo)
    nuevo, creado = await recalcular.ejecutar(anterior.id, CLAVE, "asesor", "corr-1")
    repetido, creado_otra_vez = await recalcular.ejecutar(anterior.id, CLAVE, "asesor", "corr-2")

    assert (creado, creado_otra_vez) == (True, False)
    assert repetido == nuevo
    assert nuevo.id != anterior.id and nuevo.perfil_anterior_id == anterior.id
    assert nuevo.resultado.estado is EstadoPerfil.CALCULADO
    assert escenario.repo.perfiles[anterior.id] == anterior  # el anterior no cambia
    assert len(escenario.repo.perfiles) == 2
    assert len(escenario.ejecutor.llamadas) == 2  # la repetición no consulta fuentes
    operaciones = [s.name for s in telemetria.lista_spans() if s.name.startswith("perfilamiento")]
    assert operaciones.count("perfilamiento.recalculo") == 2
