"""HU-W07 N1c: la respuesta de una cotización no depende de la réplica ni del objeto en memoria.

Con la misma cotización, los mismos pasos, el mismo `ahora` y la misma configuración,
`respuesta_de(...).model_dump_json(by_alias=True)` da los mismos bytes dos veces y con dos objetos
construidos por separado. La repetición de la clave (200) solo cambia `pasos`, que viene vacío;
`oferta.estado` depende del `ahora` de cada solicitud. Con PostgreSQL de por medio lo comprueba la
N2 (paso 7.4): aquí el repositorio es en memoria.
"""

import copy
import json
from typing import Any

import pytest
from cotizacion.adapters.inbound.http.cotizaciones import respuesta_de

pytestmark = pytest.mark.anyio


def _bytes(caso: Any, cotizacion: Any, pasos: Any, ahora: Any) -> bytes:
    config = caso.config
    respuesta = respuesta_de(
        cotizacion,
        config.presupuesto_ms,
        pasos,
        vigencia_horas=config.oferta_vigencia_horas,
        ahora=ahora,
    )
    return respuesta.model_dump_json(by_alias=True).encode()


@pytest.mark.parametrize("con_perfil", [False, True], ids=["sin_perfil", "con_perfil"])
async def test_n1c_misma_cotizacion_mismos_bytes_con_objetos_distintos(
    escenario_cotizacion: Any, nueva_cotizacion: Any, perfil_c01: Any, con_perfil: bool
) -> None:
    caso = escenario_cotizacion.caso()
    comando = nueva_cotizacion(perfil_id=perfil_c01.id if con_perfil else None)
    cotizacion, pasos, creada = await caso.ejecutar(comando)
    assert creada and cotizacion.oferta is not None and pasos
    ahora = escenario_cotizacion.reloj.ahora()

    otra, otros_pasos = copy.deepcopy(cotizacion), copy.deepcopy(pasos)
    assert otra is not cotizacion and otra == cotizacion and otros_pasos == pasos

    primera = _bytes(caso, cotizacion, pasos, ahora)
    assert _bytes(caso, cotizacion, pasos, ahora) == primera
    assert _bytes(caso, otra, otros_pasos, ahora) == primera


async def test_n1c_la_repeticion_de_la_clave_solo_cambia_los_pasos(
    escenario_cotizacion: Any, nueva_cotizacion: Any
) -> None:
    caso = escenario_cotizacion.caso()
    creada, pasos, es_nueva = await caso.ejecutar(nueva_cotizacion())
    repetida, pasos_repetidos, es_nueva_repetida = await caso.ejecutar(nueva_cotizacion())
    assert (es_nueva, es_nueva_repetida, pasos_repetidos) == (True, False, ())
    ahora = escenario_cotizacion.reloj.ahora()

    cuerpo_201 = json.loads(_bytes(caso, creada, pasos, ahora))
    cuerpo_200 = json.loads(_bytes(caso, repetida, pasos_repetidos, ahora))
    assert cuerpo_201["pasos"] and cuerpo_200["pasos"] == []
    assert {k: v for k, v in cuerpo_201.items() if k != "pasos"} == {
        k: v for k, v in cuerpo_200.items() if k != "pasos"
    }
