"""Publica un `PolizaEmitida` v1 sintético en el SNS local para probar M10 y M11 sin esperar la tarea
17.8 de W17 (que publica el real desde `polizas`).

El evento cumple `contracts/events/PolizaEmitida.v1.schema.json` y trae solo identificadores y datos
sintéticos. Solo publica en SNS local (LocalStack de `compose.yaml`): sin endpoint, o con uno que no
sea local, se niega a publicar, así que nunca toca AWS real.

Uso (desde la raíz del repositorio, con `make dev-up`):
    uv run python scripts/dev/publicar_poliza_emitida.py --cliente-id <uuid del cliente con dispositivo>
    uv run python scripts/dev/publicar_poliza_emitida.py --cliente-id <uuid> --imprimir   # sin publicar
El tópico se crea si no existe (nombre `--topico`); suscribir la cola de `avisos` o `consulta` a él
es parte del ambiente local. Reutilizar `--event-id` publica el mismo evento otra vez (duplicado).
"""

import argparse
import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

ENDPOINT_LOCAL = "http://localhost:4566"
TOPICO_LOCAL = "solventa-local-eventos-negocio"
HOSTS_LOCALES = frozenset({"localhost", "127.0.0.1", "::1", "localstack", "host.docker.internal"})
REGION = "us-east-1"


class EndpointNoLocal(Exception):
    """El destino no es un SNS local: no se publica."""


def validar_endpoint_local(endpoint: str | None) -> str:
    """Devuelve el endpoint si es local; si falta o apunta a otro host, lanza `EndpointNoLocal`."""
    if not endpoint:
        raise EndpointNoLocal("sin endpoint: usaría AWS real")
    host = urlparse(endpoint).hostname
    if host not in HOSTS_LOCALES:
        raise EndpointNoLocal(f"el endpoint {host!r} no es local")
    return endpoint


def construir_evento(
    cliente_id: uuid.UUID,
    *,
    event_id: uuid.UUID | None = None,
    poliza_id: uuid.UUID | None = None,
    ahora: datetime | None = None,
) -> dict[str, Any]:
    """El sobre de `PolizaEmitida` v1 (contracts/events/PolizaEmitida.v1.schema.json)."""
    ahora = ahora or datetime.now(UTC)
    poliza_id = poliza_id or uuid.uuid4()
    emitida = ahora.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    desde = ahora.date() + timedelta(days=1)
    hasta = desde.replace(year=desde.year + 1) - timedelta(days=1)
    return {
        "eventId": str(event_id or uuid.uuid4()),
        "aggregateId": str(poliza_id),
        "type": "PolizaEmitida",
        "version": 1,
        "occurredAt": emitida,
        "correlationId": "corr-poliza-sintetica",
        "payload": {
            "polizaId": str(poliza_id),
            "numero": "100-2026-000001",
            "cotizacionId": str(uuid.uuid4()),
            "claveRecaudo": str(uuid.uuid4()),
            "clienteId": str(cliente_id),
            "producto": {"id": "vida-deudor", "version": "1.0.0"},
            "vigencia": {"desde": desde.isoformat(), "hasta": hasta.isoformat()},
            "prima": "64638.10",
            "sumaAsegurada": "50000000",
            "moneda": "COP",
            "periodicidad": "MENSUAL",
            "documento": {
                "clave": f"polizas/{poliza_id}.pdf",
                "versionId": "v1",
                "huella": "sha256:" + "a" * 64,
                "tamanoBytes": 2048,
            },
            "firma": {"firmaId": "FIRMA-SINT-0001", "selloTiempo": emitida},
            "emitidaEn": emitida,
        },
    }


def publicar(cliente_sns: Any, topic_arn: str, evento: dict[str, Any]) -> None:
    cliente_sns.publish(
        TopicArn=topic_arn,
        Message=json.dumps(evento),
        MessageAttributes={"eventType": {"DataType": "String", "StringValue": evento["type"]}},
    )


def _argumentos(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--cliente-id", type=uuid.UUID, default=None, help="UUID del cliente")
    parser.add_argument("--event-id", type=uuid.UUID, default=None, help="repetirlo = duplicado")
    parser.add_argument("--poliza-id", type=uuid.UUID, default=None)
    parser.add_argument("--endpoint", default=os.environ.get("SOLVENTA_AWS_ENDPOINT_URL"))
    parser.add_argument("--topico", default=TOPICO_LOCAL)
    parser.add_argument("--imprimir", action="store_true", help="solo imprime el evento")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _argumentos(sys.argv[1:] if argv is None else argv)
    evento = construir_evento(
        args.cliente_id or uuid.uuid4(), event_id=args.event_id, poliza_id=args.poliza_id
    )
    if args.imprimir:
        print(json.dumps(evento, indent=2))
        return 0
    try:
        endpoint = validar_endpoint_local(args.endpoint or ENDPOINT_LOCAL)
    except EndpointNoLocal as error:
        print(f"No se publica: {error}. Este script solo publica en SNS local.", file=sys.stderr)
        return 2
    import boto3

    # LocalStack acepta cualquier credencial; no se usan las del entorno del desarrollador.
    cliente = boto3.client(
        "sns",
        region_name=REGION,
        endpoint_url=endpoint,
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    topic_arn = cliente.create_topic(Name=args.topico)["TopicArn"]
    publicar(cliente, topic_arn, evento)
    print(
        f"PolizaEmitida publicado: eventId={evento['eventId']} "
        f"clienteId={evento['payload']['clienteId']} tópico={topic_arn}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
