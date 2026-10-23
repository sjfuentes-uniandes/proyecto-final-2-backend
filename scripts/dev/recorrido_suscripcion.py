"""Recorrido local de HU-W13 por HTTP contra `make dev-up` (guía docs/hu-w13-suscripcion.md).

Por cada caso sintético de contracts/datos-sinteticos/cotizaciones-suscripcion.json que se
reproduce en local: calcula el perfil (si el caso lo usa) y la cotización en bff-web como asesor,
solicita la suscripción y compara el resultado con el esperado. Al final muestra la bandeja y el
resumen como suscriptor y reconstruye cada decisión en polizas. Datos 100 % sintéticos; imprime solo
identificadores, resultados y reglas.

Uso (desde la raíz del repositorio, con el ambiente arriba):
    uv run python scripts/dev/recorrido_suscripcion.py [S01 S02 ...]
Si bff-web no está en el puerto 8080, indicarlo con BFF_URL (p. ej. http://localhost:18080).
"""

import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

BFF = os.environ.get("BFF_URL", "http://localhost:8080")
POLIZAS = os.environ.get("POLIZAS_URL", "http://localhost:8088")
DATOS = (
    Path(__file__).resolve().parents[2] / "contracts/datos-sinteticos/cotizaciones-suscripcion.json"
)
POR_OMISION = ["S01", "S02", "S03", "S04", "S06", "S07", "S08", "S10", "S11"]


def _usuario(grupo: str) -> dict[str, str]:
    # En local no hay API Gateway: bff-web confía en estos encabezados (sin JWT_ISSUERS).
    return {"X-Authenticated-Sub": f"sub-{grupo}-local", "X-Authenticated-Groups": f"[{grupo}]"}


ASESOR = _usuario("asesores")
SUSCRIPTOR = _usuario("suscriptores")


def _post(cliente: httpx.Client, ruta: str, cuerpo: dict[str, Any]) -> httpx.Response:
    return cliente.post(ruta, json=cuerpo, headers={**ASESOR, "Idempotency-Key": str(uuid.uuid4())})


def _cliente_id(caso: dict[str, Any]) -> str:
    return f"00000000-0000-4000-8000-{int(caso['cliente'][1:]):012x}"


def recorrer(cliente: httpx.Client, caso: dict[str, Any]) -> dict[str, Any] | None:
    cotizacion = caso["cotizacion"]
    datos = cotizacion["datosAsegurado"]
    cuerpo: dict[str, Any] = {
        "clienteId": _cliente_id(caso),
        "productoId": cotizacion["productoId"],
        "datosAsegurado": datos,
    }
    if cotizacion["conPerfil"]:
        perfil = _post(
            cliente,
            "/v1/perfiles",
            {
                "clienteId": cuerpo["clienteId"],
                "productoId": cotizacion["productoId"],
                "datosDeclarados": datos,
            },
        )
        perfil.raise_for_status()
        cuerpo["perfilId"] = perfil.json()["id"]
    creada = _post(cliente, "/v1/cotizaciones", cuerpo)
    creada.raise_for_status()
    respuesta = _post(cliente, "/v1/suscripciones", {"cotizacionId": creada.json()["id"]})
    esperado = caso["esperado"]
    cuerpo_respuesta = respuesta.json()
    obtenido = cuerpo_respuesta.get("resultado") or cuerpo_respuesta.get("codigo")
    meta = esperado.get("resultado") or esperado.get("codigo")
    no_cumplen = [
        f"{r['codigo']}={r['resultado']}"
        for r in cuerpo_respuesta.get("reglasEvaluadas", [])
        if r["resultado"] != "CUMPLE"
    ]
    estado = "OK " if (respuesta.status_code, obtenido) == (esperado["http"], meta) else "MAL"
    print(
        f"{estado} {caso['id']} {caso['nombre']}: HTTP {respuesta.status_code} {obtenido} "
        f"(esperado {esperado['http']} {meta}) determinante={cuerpo_respuesta.get('reglaDeterminante')} "
        f"no_cumplen={no_cumplen} codigo={cuerpo_respuesta.get('codigo')} "
        f"correlacion={respuesta.headers.get('X-Correlation-Id')}"
    )
    return cuerpo_respuesta if respuesta.status_code in (200, 201) else None


def main(ids: list[str]) -> int:
    casos = {c["id"]: c for c in json.loads(DATOS.read_text(encoding="utf-8"))["casos"]}
    decisiones = []
    with httpx.Client(base_url=BFF, timeout=10) as cliente:
        for id_caso in ids or POR_OMISION:
            decision = recorrer(cliente, casos[id_caso])
            if decision:
                decisiones.append(decision)
        ahora = datetime.now(UTC)
        dia = {"desde": (ahora - timedelta(days=1)).isoformat(), "hasta": ahora.isoformat()}
        bandeja = cliente.get("/v1/bandeja-suscripcion", headers=SUSCRIPTOR).json()
        print(f"Bandeja: total={bandeja['total']}")
        for caso in bandeja["items"]:
            print(
                f"  {caso['codigo']} cliente={caso['clienteNombre']} reglas={caso['reglasNoCumplidas']} "
                f"motivos={caso['motivos']} en_cola={caso['antiguedadMinutos']} min"
            )
        resumen = cliente.get("/v1/bandeja-suscripcion/resumen", params=dia, headers=SUSCRIPTOR)
        print(f"Resumen: {resumen.json()}")
    with httpx.Client(base_url=POLIZAS, timeout=10) as polizas:
        for decision in decisiones:
            r = polizas.get(
                f"/v1/suscripciones/{decision['id']}/reconstruccion",
                headers={"X-Actor-Id": "sub-suscriptores-local", "X-Actor-Rol": "SUSCRIPTOR"},
            ).json()
            print(
                f"Reconstrucción {decision['codigo']}: coincide={r['coincide']} {r['resultadoOriginal']}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
