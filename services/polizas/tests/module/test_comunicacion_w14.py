"""MT-W14-01: `comunicacion` en el GET y el POST de polizas con PostgreSQL efímero (Testcontainers)
y dobles HTTP de cotizacion y catalogo (HU-W14, criterios técnicos 1 a 4 de §2.2).

La comunicación no se persiste: la fila de la decisión y su evento no cambian, y una aprobada
deja de admitir contratación al vencer. Si catalogo no responde al leer, la decisión sale con
`alternativas = []`.
"""

from datetime import timedelta
from typing import Any
from uuid import uuid4

import psycopg
import pytest

pytestmark = pytest.mark.anyio
BASE = "/v1/suscripciones"
ASESOR = {"X-Actor-Id": "sub-asesor-1", "X-Actor-Rol": "ASESOR"}
SUSCRIPTOR = {"X-Actor-Id": "sub-suscriptor-1", "X-Actor-Rol": "SUSCRIPTOR"}
# Caso → (condición, admite contratación, siguiente paso, motivo, número de alternativas).
ESPERADO = {
    "S01": ("APROBADA_SIN_AJUSTES", True, "ACEPTAR_OFERTA", None, 0),
    "S02": ("NO_ASEGURABLE", False, "COTIZAR_ALTERNATIVA", "EDAD_FUERA_DE_RANGO", 3),
    "S03": ("NO_ASEGURABLE", False, "COTIZAR_ALTERNATIVA", "PLAZO_FUERA_DE_RANGO", 3),
    "S04": ("NO_ASEGURABLE", False, "COTIZAR_ALTERNATIVA", "NO_ELEGIBLE", 3),
    "S06": ("EN_REVISION", False, "ESPERAR_REVISION", "SUMA_REQUIERE_REVISION", 0),
    "S07": ("EN_REVISION", False, "ESPERAR_REVISION", "PERFIL_REQUIERE_REVISION", 0),
    "S08": ("EN_REVISION", False, "ESPERAR_REVISION", "PERFIL_REQUIERE_REVISION", 0),
}


def filas(url: str) -> list[tuple[Any, ...]]:
    """Todas las decisiones y los eventos guardados, para comprobar que leer no escribe."""
    with psycopg.connect(url, autocommit=True) as conexion:
        decisiones = conexion.execute(
            "SELECT * FROM decisiones_suscripcion ORDER BY decidida_en"
        ).fetchall()
        eventos = conexion.execute("SELECT * FROM outbox ORDER BY 1").fetchall()
    return [*decisiones, *eventos]


async def _solicitar(api: Any, sinteticos: Any, caso_id: str) -> Any:
    return await api.post(
        BASE,
        json={"cotizacionId": sinteticos.cotizacion_id(caso_id)},
        headers={**ASESOR, "Idempotency-Key": str(uuid4())},
    )


def _resumen(comunicacion: dict[str, Any]) -> tuple[Any, ...]:
    return (
        comunicacion["condicion"],
        comunicacion["admiteContratacion"],
        comunicacion["siguientePaso"],
        comunicacion["motivoCliente"],
        len(comunicacion["alternativas"]),
    )


async def test_post_y_get_devuelven_la_comunicacion_de_cada_resultado(
    api: Any, sinteticos: Any
) -> None:
    """Criterios técnicos 1 a 3 con la base real: el POST y el GET (asesor y suscriptor)
    comunican lo mismo."""
    for caso_id, esperado in ESPERADO.items():
        creada = await _solicitar(api, sinteticos, caso_id)
        assert creada.status_code == 201, caso_id
        cuerpo = creada.json()
        assert _resumen(cuerpo["comunicacion"]) == esperado, caso_id
        for actor in (ASESOR, SUSCRIPTOR):
            leida = await api.get(f"{BASE}/{cuerpo['id']}", headers=actor)
            assert leida.json()["comunicacion"] == cuerpo["comunicacion"], caso_id
        plazo = cuerpo["comunicacion"]["plazoRespuestaHoras"]
        assert plazo == (8 if esperado[0] == "EN_REVISION" else None), caso_id


async def test_la_aprobada_vence_al_leer_sin_cambiar_la_fila(
    api: Any, sinteticos: Any, dobles: Any, limpia: str
) -> None:
    """Criterio técnico 4: la comunicación se deriva con el reloj; la decisión guardada y su
    evento no cambian."""
    creada = (await _solicitar(api, sinteticos, "S01")).json()
    antes = filas(limpia)
    dobles.reloj.instante = sinteticos.insumos("S01").vigente_hasta + timedelta(seconds=1)
    leida = (await api.get(f"{BASE}/{creada['id']}", headers=ASESOR)).json()
    assert (
        leida["comunicacion"]["admiteContratacion"],
        leida["comunicacion"]["siguientePaso"],
        leida["comunicacion"]["vigenteHasta"],
    ) == (False, "COTIZAR_NUEVAMENTE", creada["condiciones"]["vigenteHasta"])
    assert filas(limpia) == antes


async def test_catalogo_caido_al_leer_devuelve_la_decision_sin_alternativas(
    api: Any, sinteticos: Any, abrir_api: Any
) -> None:
    """Falla abierta solo para las alternativas: otra réplica (sin caché) con catalogo caído
    responde 200 con la decisión y `alternativas = []`."""
    creada = (await _solicitar(api, sinteticos, "S02")).json()
    assert len(creada["comunicacion"]["alternativas"]) == 3
    proveedores = api.proveedores
    proveedores.catalogo = "caido"
    async with abrir_api(proveedores) as otra_replica:
        leida = await otra_replica.get(f"{BASE}/{creada['id']}", headers=ASESOR)
    assert leida.status_code == 200
    assert leida.json()["resultado"] == "RECHAZADA"
    assert _resumen(leida.json()["comunicacion"]) == (
        "NO_ASEGURABLE",
        False,
        "COTIZAR_ALTERNATIVA",
        "EDAD_FUERA_DE_RANGO",
        0,
    )
