"""`scripts/dev/firmar_evento.py`: la fuente sintética de los recorridos locales y de INT. Lo que
firma debe aceptarlo el servicio, y nunca imprime el secreto ni la firma."""

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from adaptador_telemetria.config import Settings
from adaptador_telemetria.domain.telemetria.firma import calcular_firma
from adaptador_telemetria.main import crear_app
from fastapi.testclient import TestClient

RUTA = Path(__file__).resolve().parents[4] / "scripts" / "dev" / "firmar_evento.py"
SECRETO = "secreto-de-prueba"
AHORA = datetime(2026, 11, 10, 14, 0, 5, tzinfo=UTC)


@pytest.fixture(scope="module")
def script() -> ModuleType:
    especificacion = importlib.util.spec_from_file_location("firmar_evento", RUTA)
    assert especificacion and especificacion.loader
    modulo = importlib.util.module_from_spec(especificacion)
    sys.modules["firmar_evento"] = modulo
    especificacion.loader.exec_module(modulo)
    return modulo


def test_el_secreto_sale_del_json_de_credenciales(script: ModuleType) -> None:
    assert script.secreto_de('{"hmac_secret": "s"}') == "s"
    for malo in (None, "", "no es json", "{}", '{"hmac_secret": ""}', "[]"):
        with pytest.raises(SystemExit):
            script.secreto_de(malo)


def test_el_envio_firmado_es_el_que_verifica_el_servicio(
    script: ModuleType, casos: dict[str, dict[str, Any]]
) -> None:
    encabezados, cuerpo = script.armar_envio(casos["valido"], AHORA, SECRETO)
    marca = encabezados["X-Telemetria-Marca-Tiempo"]
    assert marca == "2026-11-10T14:00:05Z"
    assert encabezados["X-Telemetria-Firma"] == calcular_firma(SECRETO, marca, cuerpo)
    assert json.loads(cuerpo)["idExterno"] == "IDEAM-2026-1110-0001"


def test_los_casos_de_firma_alterada_ausente_y_marca_vieja(
    script: ModuleType, casos: dict[str, dict[str, Any]]
) -> None:
    valida, cuerpo = script.armar_envio(casos["valido"], AHORA, SECRETO)
    alterada, _ = script.armar_envio(casos["firmaInvalida"], AHORA, SECRETO)
    assert alterada["X-Telemetria-Firma"] != calcular_firma(
        SECRETO,
        alterada["X-Telemetria-Marca-Tiempo"],
        json.dumps(casos["firmaInvalida"]["envio"]["cuerpo"], separators=(",", ":")).encode(),
    )
    ausente, _ = script.armar_envio(casos["sinFirma"], AHORA, SECRETO)
    assert "X-Telemetria-Firma" not in ausente
    vieja, _ = script.armar_envio(casos["marcaDeTiempoVieja"], AHORA, SECRETO)
    assert vieja["X-Telemetria-Marca-Tiempo"] == "2026-11-10T13:55:04Z"
    assert valida["X-Telemetria-Fuente"] == "IDEAM-SIM" and cuerpo


def test_el_sufijo_cambia_el_id_externo_y_por_lo_tanto_el_evento(
    script: ModuleType, casos: dict[str, dict[str, Any]]
) -> None:
    con = script.con_sufijo(casos["valido"], "corrida-42")
    assert con["envio"]["cuerpo"]["idExterno"] == "IDEAM-2026-1110-0001-corrida-42"
    assert casos["valido"]["envio"]["cuerpo"]["idExterno"] == "IDEAM-2026-1110-0001"
    assert script.con_sufijo(casos["valido"], "") is casos["valido"]
    cruda = script.con_sufijo(casos["malformadoNoJson"], "x")
    assert "cuerpo" not in cruda["envio"] or cruda["envio"].get("cuerpoCrudo")


def test_contra_el_servicio_cada_caso_responde_lo_esperado_y_no_imprime_secretos(
    script: ModuleType,
    datos: dict[str, Any],
    archivo: Any,
    registro: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings(ALLY_TELEMETRIA_CREDENTIALS=json.dumps({"hmac_secret": SECRETO}))
    app = crear_app(settings, archivo=archivo, registro=registro)
    with TestClient(app) as servicio:
        transporte = httpx.MockTransport(
            lambda peticion: httpx.Response(
                (
                    r := servicio.post(
                        "/v1/eventos", content=peticion.content, headers=dict(peticion.headers)
                    )
                ).status_code,
                content=r.content,
            )
        )
        with httpx.Client(transport=transporte) as cliente:
            resultados = [
                script.enviar_caso(cliente, "http://servicio/v1/eventos", caso, SECRETO)
                for caso in datos["eventos"]
            ]
    malos = [linea for bien, linea in resultados if not bien]
    assert malos == []
    texto = "\n".join(linea for _, linea in resultados)
    assert SECRETO not in texto and "sha256=" not in texto
    # (La salida estándar es del servicio en proceso, no del script, que solo devuelve la línea.)
    assert "sha256=" not in capsys.readouterr().out


def test_sin_casos_pide_que_se_elija(script: ModuleType) -> None:
    with pytest.raises(SystemExit):
        script.main(["--secreto", "s"])
    with pytest.raises(SystemExit):
        script.main(["--secreto", "s", "--caso", "no-existe"])
