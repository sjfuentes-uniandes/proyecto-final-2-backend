import importlib.util
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

_spec = importlib.util.spec_from_file_location(
    "publicar_poliza_emitida", Path(__file__).with_name("publicar_poliza_emitida.py")
)
assert _spec is not None and _spec.loader is not None
script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)

ESQUEMA = json.loads(
    (Path(__file__).parents[2] / "contracts" / "events" / "PolizaEmitida.v1.schema.json").read_text(
        encoding="utf-8"
    )
)


def test_el_evento_generado_cumple_el_esquema_de_poliza_emitida_v1() -> None:
    cliente = uuid.uuid4()
    evento = script.construir_evento(cliente)
    validador = Draft202012Validator(ESQUEMA, format_checker=Draft202012Validator.FORMAT_CHECKER)
    assert list(validador.iter_errors(evento)) == []
    assert evento["payload"]["clienteId"] == str(cliente)


def test_con_los_mismos_ids_el_evento_es_el_mismo_duplicado() -> None:
    event_id, poliza_id = uuid.uuid4(), uuid.uuid4()
    uno = script.construir_evento(uuid.uuid4(), event_id=event_id, poliza_id=poliza_id)
    otro = script.construir_evento(uuid.uuid4(), event_id=event_id, poliza_id=poliza_id)
    assert uno["eventId"] == otro["eventId"] and uno["aggregateId"] == otro["aggregateId"]


@pytest.mark.parametrize(
    "endpoint", [None, "", "https://sns.us-east-1.amazonaws.com", "http://10.0.0.5:4566"]
)
def test_no_publica_si_el_endpoint_no_es_local(endpoint: str | None) -> None:
    with pytest.raises(script.EndpointNoLocal):
        script.validar_endpoint_local(endpoint)


def test_el_endpoint_local_se_acepta() -> None:
    assert script.validar_endpoint_local("http://localhost:4566") == "http://localhost:4566"
    assert script.validar_endpoint_local("http://localstack:4566") == "http://localstack:4566"


def test_main_rechaza_un_endpoint_de_aws_sin_publicar(capsys: pytest.CaptureFixture[str]) -> None:
    codigo = script.main(["--endpoint", "https://sns.us-east-1.amazonaws.com"])
    assert codigo == 2
    assert "solo publica en SNS local" in capsys.readouterr().err


def test_publicar_envia_el_evento_con_su_event_type() -> None:
    class SnsFalso:
        def __init__(self) -> None:
            self.llamadas: list[dict[str, Any]] = []

        def publish(self, **kwargs: Any) -> None:
            self.llamadas.append(kwargs)

    sns = SnsFalso()
    evento = script.construir_evento(uuid.uuid4())
    script.publicar(sns, "arn:aws:sns:us-east-1:000000000000:t", evento)
    [llamada] = sns.llamadas
    assert json.loads(llamada["Message"]) == evento
    assert llamada["MessageAttributes"]["eventType"]["StringValue"] == "PolizaEmitida"
