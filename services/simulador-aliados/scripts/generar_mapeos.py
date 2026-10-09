"""Genera los mapeos de WireMock desde contracts/datos-sinteticos/clientes-perfilamiento.json.

Uso (desde la raíz del repositorio):
    python services/simulador-aliados/scripts/generar_mapeos.py clientes-stub

Los mapeos generados se versionan; la prueba tests/test_generar_mapeos.py verifica que
coincidan con lo que genera este script. Solo usa la biblioteca estándar.
"""

import argparse
import json
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

SIMULADOR = Path(__file__).resolve().parents[1]
RAIZ = SIMULADOR.parents[1]
DATOS = RAIZ / "contracts" / "datos-sinteticos" / "clientes-perfilamiento.json"

ALCANCES = (
    "FA_PRODUCTOS_VIGENTES",
    "FA_HISTORIAL_PAGOS_12M",
    "FA_INGRESOS_AGREGADOS",
    "DA_REGISTROS_PUBLICOS",
)
PROPOSITO = "PERFILAMIENTO_PRECIO"
VIGENTE_HASTA = "2027-10-01T00:00:00Z"
VERIFICADO_EN = "2026-10-01T00:00:00Z"
PRIORIDAD_RESPALDO = 10
JSON = {"Content-Type": "application/json"}

Archivos = dict[str, Any]


def cargar_casos(ruta: Path = DATOS) -> list[dict[str, Any]]:
    return json.loads(ruta.read_text(encoding="utf-8"))


def _nombre(caso: Mapping[str, Any]) -> str:
    return str(caso["id"]).lower()


# --- clientes-stub (02, CS-08) -------------------------------------------------------
def _base_clientes(cliente_id: str) -> str:
    return f"/clientes-stub/v1/clientes/{cliente_id}"


def _identidad(caso: Mapping[str, Any]) -> dict[str, Any]:
    estado = caso["identidad"]
    return {
        "name": f"clientes-stub {caso['id']} identidad",
        "request": {"method": "GET", "urlPath": f"{_base_clientes(caso['clienteId'])}/identidad"},
        "response": {
            "status": 200,
            "headers": JSON,
            "jsonBody": {
                "clienteId": caso["clienteId"],
                "estado": estado,
                "verificadoEn": VERIFICADO_EN if estado == "VERIFICADO" else None,
            },
        },
    }


def _verificacion(caso: Mapping[str, Any]) -> dict[str, Any]:
    consentimiento = caso["consentimiento"]
    decision = consentimiento["decision"]
    permitidos = list(consentimiento["alcancesPermitidos"])
    return {
        "name": f"clientes-stub {caso['id']} verificación de consentimiento",
        "request": {
            "method": "POST",
            "urlPath": f"{_base_clientes(caso['clienteId'])}/consentimientos/verificaciones",
            "bodyPatterns": [{"matchesJsonPath": f"$[?(@.proposito == '{PROPOSITO}')]"}],
        },
        "response": {
            "status": 200,
            "headers": JSON,
            "jsonBody": {
                "decision": decision,
                "consentimientoId": consentimiento["consentimientoId"],
                "vigenteHasta": None if decision == "RECHAZADO" else VIGENTE_HASTA,
                "alcancesPermitidos": permitidos,
                "alcancesRechazados": [
                    {"alcance": alcance, "causa": "ALCANCE_NO_AUTORIZADO"}
                    for alcance in ALCANCES
                    if alcance not in permitidos
                ],
                "causa": consentimiento["causa"],
            },
        },
    }


def _no_encontrado(metodo: str, patron: str) -> dict[str, Any]:
    return {
        "name": f"clientes-stub {metodo} cliente no encontrado",
        "priority": PRIORIDAD_RESPALDO,
        "request": {"method": metodo, "urlPathPattern": patron},
        "response": {
            "status": 404,
            "headers": JSON,
            "jsonBody": {
                "codigo": "CLIENTE_NO_ENCONTRADO",
                "mensaje": "Cliente no encontrado",
                "correlationId": None,
                "detalles": {},
            },
        },
    }


def generar_clientes_stub(casos: Iterable[Mapping[str, Any]]) -> Archivos:
    archivos: Archivos = {}
    for caso in casos:
        archivos[f"mappings/clientes-stub/{_nombre(caso)}-identidad.json"] = _identidad(caso)
        archivos[f"mappings/clientes-stub/{_nombre(caso)}-consentimiento.json"] = _verificacion(
            caso
        )
    archivos["mappings/clientes-stub/zz-no-encontrado-identidad.json"] = _no_encontrado(
        "GET", "/clientes-stub/v1/clientes/[^/]+/identidad"
    )
    archivos["mappings/clientes-stub/zz-no-encontrado-consentimiento.json"] = _no_encontrado(
        "POST", "/clientes-stub/v1/clientes/[^/]+/consentimientos/verificaciones"
    )
    return archivos


# --- Escritura ------------------------------------------------------------------------
GENERADORES = {
    "clientes-stub": (generar_clientes_stub, ("mappings/clientes-stub",)),
}


def serializar(contenido: Any) -> str:
    return json.dumps(contenido, ensure_ascii=False, indent=2) + "\n"


def carpetas_de(subcomandos: Iterable[str]) -> tuple[str, ...]:
    return tuple(carpeta for s in subcomandos for carpeta in GENERADORES[s][1])


def generar(subcomandos: Iterable[str], casos: list[dict[str, Any]]) -> Archivos:
    archivos: Archivos = {}
    for subcomando in subcomandos:
        archivos.update(GENERADORES[subcomando][0](casos))
    return archivos


def escribir(archivos: Archivos, carpetas: Iterable[str], destino: Path = SIMULADOR) -> None:
    """Reemplaza los .json de las carpetas generadas (conserva .gitkeep)."""
    for carpeta in carpetas:
        directorio = destino / carpeta
        directorio.mkdir(parents=True, exist_ok=True)
        for anterior in directorio.glob("*.json"):
            anterior.unlink()
    for relativa, contenido in archivos.items():
        (destino / relativa).write_text(serializar(contenido), encoding="utf-8")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("subcomando", choices=[*GENERADORES, "todo"])
    args = parser.parse_args(argv)
    subcomandos = list(GENERADORES) if args.subcomando == "todo" else [args.subcomando]
    archivos = generar(subcomandos, cargar_casos())
    escribir(archivos, carpetas_de(subcomandos))
    print(f"{len(archivos)} archivos generados en {SIMULADOR.relative_to(RAIZ)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
