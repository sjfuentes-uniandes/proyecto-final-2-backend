from collections.abc import Callable
from datetime import UTC, date, datetime
from io import StringIO

import pytest
from api_socios.application.socios import AdministrarSocios
from api_socios.domain.cuotas import EstadoCuota, PlanNoDisponible
from api_socios.domain.socios import (
    CanalVenta,
    NuevoSocio,
    Permiso,
    Plan,
    SocioDesactivado,
    SocioNoEncontrado,
)

AHORA = datetime(2026, 10, 13, 15, 30, tzinfo=UTC)


def nuevo(nombre: str = "Retail Andino", plan: Plan = Plan.BASICO) -> NuevoSocio:
    return NuevoSocio(nombre, CanalVenta.COMERCIO, plan, frozenset({Permiso.COTIZAR}))


class MetricasFalsas:
    def __init__(self, limitadas: dict[str, int] | None = None, falla: bool = False) -> None:
        self.valores = limitadas or {}
        self.falla = falla
        self.consultas: list[tuple[str, datetime, datetime]] = []

    def limitadas(self, api_key_id: str, desde: datetime, hasta: datetime) -> int:
        self.consultas.append((api_key_id, desde, hasta))
        if self.falla:
            raise ConnectionError("cloudwatch no disponible")
        return self.valores.get(api_key_id, 0)


def test_cambiar_plan_mueve_la_api_key_y_persiste(entorno) -> None:
    socio, _ = entorno.caso.habilitar(nuevo())
    actualizado = entorno.caso.cambiar_plan(socio.id, Plan.PRUEBA)
    assert actualizado.plan is Plan.PRUEBA
    assert entorno.api_keys.planes == {socio.api_key_id: Plan.PRUEBA}
    assert entorno.repositorio.obtener(socio.id) == actualizado
    assert (actualizado.client_id, actualizado.api_key_id) == (socio.client_id, socio.api_key_id)


def test_cambiar_al_mismo_plan_no_toca_api_gateway(entorno) -> None:
    socio, _ = entorno.caso.habilitar(nuevo(plan=Plan.ESTANDAR))
    entorno.api_keys.falla_desasociar = True
    assert entorno.caso.cambiar_plan(socio.id, Plan.ESTANDAR) == socio


def test_plan_no_configurado_en_el_ambiente(entorno) -> None:
    socio, _ = entorno.caso.habilitar(nuevo())
    entorno.api_keys.disponibles = [Plan.BASICO, Plan.ESTANDAR]
    with pytest.raises(PlanNoDisponible):
        entorno.caso.cambiar_plan(socio.id, Plan.PRUEBA)
    assert entorno.api_keys.planes == {socio.api_key_id: Plan.BASICO}
    assert entorno.repositorio.obtener(socio.id).plan is Plan.BASICO


def test_alta_con_plan_no_configurado_no_crea_nada(entorno) -> None:
    entorno.api_keys.disponibles = [Plan.BASICO]
    with pytest.raises(PlanNoDisponible):
        entorno.caso.habilitar(nuevo(plan=Plan.PRUEBA))
    assert not entorno.identidad.clientes and not entorno.api_keys.keys
    assert not entorno.repositorio.socios


def test_no_se_cambia_el_plan_de_un_socio_desactivado(entorno) -> None:
    socio, _ = entorno.caso.habilitar(nuevo())
    entorno.caso.desactivar(socio.id)
    with pytest.raises(SocioDesactivado):
        entorno.caso.cambiar_plan(socio.id, Plan.ESTANDAR)
    assert entorno.repositorio.obtener(socio.id).plan is Plan.BASICO


def test_cambiar_plan_de_socio_inexistente(entorno) -> None:
    with pytest.raises(SocioNoEncontrado):
        entorno.caso.cambiar_plan("SOC-999999", Plan.ESTANDAR)


@pytest.mark.parametrize("falla", ["desasociar", "asociar", "base"])
def test_si_un_paso_falla_la_api_key_vuelve_al_plan_anterior(entorno, falla: str) -> None:
    socio, _ = entorno.caso.habilitar(nuevo())
    entorno.api_keys.falla_desasociar = falla == "desasociar"
    entorno.api_keys.falla_plan = Plan.ESTANDAR if falla == "asociar" else False
    entorno.repositorio.falla_al_guardar = falla == "base"
    with pytest.raises(RuntimeError):
        entorno.caso.cambiar_plan(socio.id, Plan.ESTANDAR)
    entorno.repositorio.falla_al_guardar = False
    assert entorno.api_keys.planes == {socio.api_key_id: Plan.BASICO}
    assert entorno.repositorio.obtener(socio.id).plan is Plan.BASICO


def test_compensacion_fallida_se_registra_sin_ocultar_el_error(
    entorno, monkeypatch: pytest.MonkeyPatch, capturar_logs: Callable[[], StringIO]
) -> None:
    socio, _ = entorno.caso.habilitar(nuevo())
    entorno.repositorio.falla_al_guardar = True
    original = entorno.api_keys.asociar_plan

    def asociar(api_key_id: str, plan: Plan) -> None:
        if plan is Plan.BASICO:
            raise ConnectionError("sin red")
        original(api_key_id, plan)

    monkeypatch.setattr(entorno.api_keys, "asociar_plan", asociar)
    salida = capturar_logs()
    with pytest.raises(RuntimeError, match="base no disponible"):
        entorno.caso.cambiar_plan(socio.id, Plan.ESTANDAR)
    logs = salida.getvalue()
    assert "no se pudo compensar el cambio de plan" in logs and socio.id in logs
    assert entorno.api_keys.planes == {}


def test_planes_disponibles_con_sus_limites(entorno) -> None:
    entorno.api_keys.disponibles = [Plan.BASICO, Plan.PRUEBA]
    planes = entorno.caso.planes()
    assert [(p.plan, p.limite_por_segundo, p.rafaga, p.cuota) for p in planes] == [
        (Plan.BASICO, 5, 10, 1000),
        (Plan.PRUEBA, 1, 2, 5),
    ]


def test_consumo_del_socio_en_la_ventana_vigente(entorno) -> None:
    metricas = MetricasFalsas({"key1": 2})
    caso = AdministrarSocios(entorno.repositorio, entorno.identidad, entorno.api_keys, metricas)
    socio, _ = caso.habilitar(nuevo(plan=Plan.PRUEBA))
    entorno.api_keys.usado[socio.api_key_id] = 5
    consumo = caso.consumo(socio.id, AHORA)
    assert (consumo.usado, consumo.restante, consumo.limitadas) == (5, 0, 2)
    assert consumo.estado is EstadoCuota.AGOTADA and consumo.limites.cuota == 5
    assert entorno.api_keys.consultas_uso == [
        (socio.api_key_id, Plan.PRUEBA, date(2026, 10, 13), date(2026, 10, 13))
    ]
    assert metricas.consultas == [(socio.api_key_id, datetime(2026, 10, 13, tzinfo=UTC), AHORA)]


def test_el_consumo_usa_los_limites_del_plan_nuevo(entorno) -> None:
    socio, _ = entorno.caso.habilitar(nuevo())
    entorno.api_keys.usado[socio.api_key_id] = 4
    assert entorno.caso.consumo(socio.id, AHORA).estado is EstadoCuota.NORMAL
    entorno.caso.cambiar_plan(socio.id, Plan.PRUEBA)
    consumo = entorno.caso.consumo(socio.id, AHORA)
    assert (consumo.limites.cuota, consumo.estado) == (5, EstadoCuota.CERCA_DEL_LIMITE)


def test_agotar_la_cuota_de_un_socio_no_afecta_al_otro(entorno) -> None:
    a, _ = entorno.caso.habilitar(nuevo("Socio A", Plan.PRUEBA))
    b, _ = entorno.caso.habilitar(nuevo("Socio B", Plan.PRUEBA))
    entorno.api_keys.usado[a.api_key_id] = 5
    assert entorno.caso.consumo(a.id, AHORA).estado is EstadoCuota.AGOTADA
    consumo_b = entorno.caso.consumo(b.id, AHORA)
    assert (consumo_b.usado, consumo_b.restante, consumo_b.estado) == (0, 5, EstadoCuota.NORMAL)
    assert {consulta[0] for consulta in entorno.api_keys.consultas_uso} == {
        a.api_key_id,
        b.api_key_id,
    }


def test_sin_metricas_el_consumo_sigue_disponible(
    entorno, capturar_logs: Callable[[], StringIO]
) -> None:
    caso = AdministrarSocios(
        entorno.repositorio, entorno.identidad, entorno.api_keys, MetricasFalsas(falla=True)
    )
    socio, _ = caso.habilitar(nuevo())
    salida = capturar_logs()
    assert caso.consumo(socio.id, AHORA).limitadas is None
    assert entorno.caso.consumo(socio.id, AHORA).limitadas is None
    assert "no se pudieron leer las limitaciones del socio" in salida.getvalue()


def test_consumo_de_socio_inexistente(entorno) -> None:
    with pytest.raises(SocioNoEncontrado):
        entorno.caso.consumo("SOC-999999")
