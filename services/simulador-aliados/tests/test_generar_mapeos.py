"""Los mapeos versionados son los que genera el script (02, CS-08 y 03, W10-P06).

Se corre con: uv run --with pytest pytest services/simulador-aliados/tests
Si falla, regenerar con: python services/simulador-aliados/scripts/generar_mapeos.py todo
"""

import importlib.util
import json
from pathlib import Path

import pytest

SIMULADOR = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "generar_mapeos", SIMULADOR / "scripts" / "generar_mapeos.py"
)
assert _spec is not None and _spec.loader is not None
gm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gm)

CASOS = gm.cargar_casos()
SUBCOMANDOS = list(gm.GENERADORES)


def _versionados(carpetas: tuple[str, ...]) -> dict[str, str]:
    return {
        str(ruta.relative_to(SIMULADOR)): ruta.read_text(encoding="utf-8")
        for carpeta in carpetas
        for ruta in sorted((SIMULADOR / carpeta).glob("*.json"))
    }


@pytest.mark.parametrize("subcomando", SUBCOMANDOS)
def test_mapeos_versionados_identicos_a_los_generados(subcomando: str) -> None:
    generados = {ruta: gm.serializar(c) for ruta, c in gm.generar([subcomando], CASOS).items()}
    assert _versionados(gm.carpetas_de([subcomando])) == generados


def test_escribir_reemplaza_los_anteriores(tmp_path: Path) -> None:
    carpeta = tmp_path / "mappings" / "clientes-stub"
    carpeta.mkdir(parents=True)
    (carpeta / "viejo.json").write_text("{}", encoding="utf-8")
    (carpeta / ".gitkeep").write_text("", encoding="utf-8")
    archivos = gm.generar(["clientes-stub"], CASOS)
    gm.escribir(archivos, gm.carpetas_de(["clientes-stub"]), tmp_path)
    assert not (carpeta / "viejo.json").exists()
    assert (carpeta / ".gitkeep").exists()
    assert len(list(carpeta.glob("*.json"))) == len(archivos)


def test_todos_los_mapeos_son_json_validos() -> None:
    for ruta in (SIMULADOR / "mappings").rglob("*.json"):
        json.loads(ruta.read_text(encoding="utf-8"))


def _respuesta(ruta: str) -> dict:
    return gm.generar(["clientes-stub"], CASOS)[ruta]["response"]


def test_stub_identidad_y_consentimiento_segun_los_casos() -> None:
    assert len(CASOS) == 19
    identidad = _respuesta("mappings/clientes-stub/c11-identidad.json")["jsonBody"]
    assert identidad["estado"] == "RECHAZADO" and identidad["verificadoEn"] is None
    c08 = _respuesta("mappings/clientes-stub/c08-consentimiento.json")["jsonBody"]
    assert c08["decision"] == "RECHAZADO" and c08["causa"] == "REVOCADO"
    assert c08["consentimientoId"] is None and c08["vigenteHasta"] is None
    assert c08["alcancesPermitidos"] == []
    c02 = _respuesta("mappings/clientes-stub/c02-consentimiento.json")["jsonBody"]
    assert c02["vigenteHasta"] == gm.VIGENTE_HASTA
    assert c02["alcancesRechazados"] == [
        {"alcance": "FA_INGRESOS_AGREGADOS", "causa": "ALCANCE_NO_AUTORIZADO"}
    ]


def test_stub_respaldo_404_con_menor_prioridad() -> None:
    respaldos = [m for r, m in gm.generar(["clientes-stub"], CASOS).items() if "no-encontrado" in r]
    assert len(respaldos) == 2
    for mapeo in respaldos:
        assert mapeo["priority"] == gm.PRIORIDAD_RESPALDO
        assert mapeo["response"]["status"] == 404
        assert mapeo["response"]["jsonBody"]["codigo"] == "CLIENTE_NO_ENCONTRADO"


def test_main_escribe_las_carpetas_del_subcomando(monkeypatch: pytest.MonkeyPatch) -> None:
    llamadas: list[tuple[int, tuple[str, ...]]] = []
    monkeypatch.setattr(
        gm, "escribir", lambda archivos, carpetas: llamadas.append((len(archivos), carpetas))
    )
    assert gm.main(["todo"]) == 0
    assert llamadas == [(len(gm.generar(SUBCOMANDOS, CASOS)), gm.carpetas_de(SUBCOMANDOS))]


ALIADOS = gm.generar(["aliados"], CASOS)


def _mapeo(ruta: str) -> dict:
    return ALIADOS[f"mappings/{ruta}"]


def _cuerpo(mapeo: dict) -> object:
    return ALIADOS[f"__files/{mapeo['response']['bodyFileName']}"]


def test_aliados_solicitud_exige_campos_exactos_y_api_key() -> None:
    solicitud = _mapeo("open-finance/c01-active-products.json")["request"]
    assert solicitud["urlPath"] == (
        "/open-finance/v3/customers/00000000-0000-4000-8000-000000000001/active-products"
    )
    assert solicitud["queryParameters"] == {
        "fields": {"equalTo": "creditorCount,oldestProductYears,totalMonthlyInstallment"}
    }
    assert solicitud["headers"] == {"X-Api-Key": {"matches": ".+"}}
    historial = _mapeo("open-finance/c01-payment-history.json")["request"]["queryParameters"]
    assert historial == {"months": {"equalTo": "12"}, "fields": {"equalTo": "lateCount"}}


@pytest.mark.parametrize(
    ("ruta", "estado", "demora"),
    [
        ("open-finance/c01-active-products.json", 200, 50),
        ("open-finance/c18-active-products.json", 200, 150),
        ("datos-abiertos/c06-registros.json", 200, 1000),
        ("open-finance/c13-payment-history.json", 200, 1000),
        ("open-finance/c07-aggregated-income.json", 503, 50),
    ],
)
def test_aliados_demoras_y_estados(ruta: str, estado: int, demora: int) -> None:
    respuesta = _mapeo(ruta)["response"]
    assert (respuesta["status"], respuesta["fixedDelayMilliseconds"]) == (estado, demora)


def test_aliados_variantes_del_cuerpo() -> None:
    assert _cuerpo(_mapeo("open-finance/c03-payment-history.json"))["data"] == {}
    adicional = _cuerpo(_mapeo("open-finance/c04-active-products.json"))["data"]
    assert adicional["accountNumbers"] == ["SIN-CTA-0004"]
    assert adicional["fullName"] == "Cliente Sintético 04"
    assert adicional["oldestProductYears"] == 7
    assert _cuerpo(_mapeo("open-finance/c05-active-products.json"))["data"] == "no-es-objeto"
    assert _cuerpo(_mapeo("open-finance/c07-aggregated-income.json")) == {"error": "unavailable"}
    c14 = _cuerpo(_mapeo("datos-abiertos/c14-registros.json"))["registro"]
    assert c14 == {"listasRestrictivas": {"coincidencias": 1}}
    assert _cuerpo(_mapeo("open-finance/c15-payment-history.json"))["data"] == {"lateCount": 1}


def test_aliados_solo_las_fuentes_que_usa_cada_caso() -> None:
    rutas = set(ALIADOS)
    assert not any("/c02-aggregated-income" in r for r in rutas)
    for sin_llamadas in ("c08", "c09", "c10", "c11", "c12", "c16", "c17"):
        assert not any(f"/{sin_llamadas}-" in r for r in rutas)


def test_aliados_escenario_de_recuperacion() -> None:
    primero = _mapeo("datos-abiertos/c19-registros-1-lento.json")
    segundo = _mapeo("datos-abiertos/c19-registros-2-restablecido.json")
    assert primero["scenarioName"] == segundo["scenarioName"]
    assert primero["scenarioName"] == "recuperacion-00000000-0000-4000-8000-000000000013"
    assert (primero["requiredScenarioState"], primero["newScenarioState"]) == (
        "Started",
        "Restablecido",
    )
    assert segundo["requiredScenarioState"] == "Restablecido"
    assert primero["response"]["fixedDelayMilliseconds"] == 1000
    assert segundo["response"]["fixedDelayMilliseconds"] == 50


@pytest.mark.parametrize("aliado", ["open-finance", "datos-abiertos"])
def test_aliados_respaldo_404(aliado: str) -> None:
    respaldo = _mapeo(f"{aliado}/zz-no-encontrado.json")
    assert respaldo["priority"] == gm.PRIORIDAD_RESPALDO
    assert respaldo["request"] == {"method": "ANY", "urlPathPattern": f"/{aliado}/.*"}
    assert respaldo["response"]["status"] == 404
    assert _cuerpo(respaldo) == {"error": "customer_not_found"}
