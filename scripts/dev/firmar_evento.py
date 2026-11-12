"""Firma y envía a `adaptador-telemetria` los casos de `eventos-parametricos.json` (HU-W21).

Es la fuente de telemetría sintética de los recorridos locales y de INT: arma cada envío como lo
haría una fuente real (cuerpo JSON, `X-Telemetria-Marca-Tiempo` y la firma HMAC-SHA256 sobre
`<marca>.<cuerpo crudo>`), lo envía a `POST /v1/eventos` y compara la respuesta con lo esperado del
archivo de datos. Vive fuera del código de producción: solo usa `calcular_firma` del dominio, para
no repetir el algoritmo.

El secreto sale de `ALLY_TELEMETRIA_CREDENTIALS` (JSON `{"hmac_secret": …}`, como en el servicio) o
de `--secreto`. **Nunca se imprime** ni se imprime la firma.

    export ALLY_TELEMETRIA_CREDENTIALS='{"hmac_secret":"local-sintetico"}'
    uv run python scripts/dev/firmar_evento.py --todos
    uv run python scripts/dev/firmar_evento.py --caso valido --caso duplicado
    uv run python scripts/dev/firmar_evento.py --todos --sufijo corrida-42 \\
        --url https://<api>/telemetria/v1/eventos

`--sufijo` se agrega al `idExterno` de cada caso: sin él, ejecutar dos veces el mismo caso es un
reenvío (200), no un evento nuevo (202). Termina con 1 si algún caso no responde lo esperado.
"""

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from adaptador_telemetria.domain.telemetria.firma import calcular_firma

DATOS = Path(__file__).resolve().parents[2] / "contracts/datos-sinteticos/eventos-parametricos.json"
URL_LOCAL = "http://localhost:8093/v1/eventos"


def secreto_de(credenciales: str | None) -> str:
    """El `hmac_secret` del JSON de credenciales."""
    try:
        secreto = json.loads(credenciales or "")["hmac_secret"]
    except (ValueError, KeyError, TypeError):
        secreto = None
    if not isinstance(secreto, str) or not secreto:
        raise SystemExit(
            'Falta el secreto: ALLY_TELEMETRIA_CREDENTIALS={"hmac_secret": …} o --secreto'
        )
    return secreto


def con_sufijo(caso: dict[str, Any], sufijo: str) -> dict[str, Any]:
    """El caso con `idExterno` + sufijo (si lo tiene)."""
    if not sufijo:
        return caso
    nuevo = json.loads(json.dumps(caso))
    cuerpo = nuevo["envio"].get("cuerpo")
    if cuerpo and "idExterno" in cuerpo:
        cuerpo["idExterno"] = f"{cuerpo['idExterno']}-{sufijo}"[:64]
    return nuevo


def armar_envio(
    caso: dict[str, Any], ahora: datetime, secreto: str
) -> tuple[dict[str, str], bytes]:
    """Los encabezados y el cuerpo crudo de un caso. `desfaseSegundos` es relativo a `ahora`;
    `firma` es VALIDA, ALTERADA (último dígito cambiado) o AUSENTE."""
    envio = caso["envio"]
    marca = (ahora + timedelta(seconds=envio["marcaTiempo"]["desfaseSegundos"])).astimezone(UTC)
    marca_texto = marca.isoformat(timespec="seconds").replace("+00:00", "Z")
    if "cuerpoCrudo" in envio:
        cuerpo = envio["cuerpoCrudo"].encode()
    else:
        cuerpo = json.dumps(envio["cuerpo"], separators=(",", ":")).encode()
    encabezados = {"X-Telemetria-Fuente": envio["fuente"], "X-Telemetria-Marca-Tiempo": marca_texto}
    firma = calcular_firma(secreto, marca_texto, cuerpo)
    if envio["firma"] == "ALTERADA":
        firma = firma[:-1] + ("0" if firma[-1] != "0" else "1")
    if envio["firma"] != "AUSENTE":
        encabezados["X-Telemetria-Firma"] = firma
    return encabezados, cuerpo


def enviar_caso(
    cliente: httpx.Client, url: str, caso: dict[str, Any], secreto: str
) -> tuple[bool, str]:
    """Envía un caso y dice si la respuesta es la esperada, con una línea sin secretos."""
    encabezados, cuerpo = armar_envio(caso, datetime.now(UTC), secreto)
    respuesta = cliente.post(
        url, content=cuerpo, headers={**encabezados, "Content-Type": "application/json"}
    )
    esperado = caso["esperado"]["http"]
    datos = respuesta.json() if respuesta.content else {}
    codigo = datos.get("codigo")
    bien = respuesta.status_code == esperado["estado"] and codigo == esperado["codigo"]
    detalle = datos.get("eventoId") or codigo or ""
    return bien, (
        f"{'OK ' if bien else 'MAL'} {caso['clave']:<32} {respuesta.status_code} "
        f"(esperado {esperado['estado']}) {detalle}"
    )


def main(argv: list[str] | None = None) -> int:
    analizador = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    analizador.add_argument("--url", default=URL_LOCAL)
    analizador.add_argument("--datos", type=Path, default=DATOS)
    analizador.add_argument(
        "--caso", action="append", default=[], help="clave del caso (repetible)"
    )
    analizador.add_argument("--todos", action="store_true")
    analizador.add_argument("--sufijo", default="", help="se agrega al idExterno de cada caso")
    analizador.add_argument(
        "--secreto", help="el hmac_secret; por omisión, ALLY_TELEMETRIA_CREDENTIALS"
    )
    argumentos = analizador.parse_args(argv)

    secreto = argumentos.secreto or secreto_de(os.environ.get("ALLY_TELEMETRIA_CREDENTIALS"))
    casos = json.loads(argumentos.datos.read_text(encoding="utf-8"))["eventos"]
    por_clave = {c["clave"]: c for c in casos}
    if argumentos.todos:
        elegidos = casos
    elif argumentos.caso:
        desconocidos = [c for c in argumentos.caso if c not in por_clave]
        if desconocidos:
            raise SystemExit(f"Casos desconocidos: {', '.join(desconocidos)}")
        elegidos = [por_clave[c] for c in argumentos.caso]
    else:
        raise SystemExit(
            "Indique --todos o al menos un --caso (claves: " + ", ".join(por_clave) + ")"
        )

    todos_bien = True
    with httpx.Client(timeout=10) as cliente:
        for caso in elegidos:
            bien, linea = enviar_caso(
                cliente, argumentos.url, con_sufijo(caso, argumentos.sufijo), secreto
            )
            print(linea)
            todos_bien &= bien
    return 0 if todos_bien else 1


if __name__ == "__main__":
    sys.exit(main())
