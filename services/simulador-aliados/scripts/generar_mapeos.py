"""Genera los mapeos de WireMock desde contracts/datos-sinteticos/clientes-perfilamiento.json.

Uso (desde la raíz del repositorio):
    python services/simulador-aliados/scripts/generar_mapeos.py {clientes-stub|aliados|todo}

Los mapeos generados se versionan; la prueba tests/test_generar_mapeos.py verifica que
coincidan con lo que genera este script. Solo usa la biblioteca estándar.
"""

import argparse
import copy
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


# --- aliados: open-finance y datos-abiertos (03, W10-P06) ------------------------------
# Fuente -> (aliado, recurso, ruta de la solicitud, parámetros fijos, nominal de 01 §3.6).
FUENTES: dict[str, dict[str, Any]] = {
    "FA_PRODUCTOS_VIGENTES": {
        "aliado": "open-finance",
        "recurso": "active-products",
        "ruta": "/open-finance/v3/customers/{id}/active-products",
        "parametros": {"fields": "creditorCount,oldestProductYears,totalMonthlyInstallment"},
        "contenedor": "data",
        "nominal": {
            "schemaVersion": "3.1",
            "confidenceScore": 0.95,
            "data": {
                "oldestProductYears": 7,
                "totalMonthlyInstallment": {"amount": "1800000.00", "currency": "COP"},
                "creditorCount": 2,
            },
        },
    },
    "FA_HISTORIAL_PAGOS_12M": {
        "aliado": "open-finance",
        "recurso": "payment-history",
        "ruta": "/open-finance/v3/customers/{id}/payment-history",
        "parametros": {"months": "12", "fields": "lateCount"},
        "contenedor": "data",
        "nominal": {"schemaVersion": "3.1", "confidenceScore": 0.91, "data": {"lateCount": 0}},
    },
    "FA_INGRESOS_AGREGADOS": {
        "aliado": "open-finance",
        "recurso": "aggregated-income",
        "ruta": "/open-finance/v3/customers/{id}/aggregated-income",
        "parametros": {"fields": "estimatedMonthlyIncome"},
        "contenedor": "data",
        "nominal": {
            "schemaVersion": "3.1",
            "confidenceScore": 0.93,
            "data": {"estimatedMonthlyIncome": {"amount": "6500000.00", "currency": "COP"}},
        },
    },
    "DA_REGISTROS_PUBLICOS": {
        "aliado": "datos-abiertos",
        "recurso": "registros",
        "ruta": "/datos-abiertos/v1/registros/{id}",
        "parametros": {"campos": "listasRestrictivas"},
        "contenedor": "registro",
        "nominal": {
            "version": "1.8",
            "nivelConfianza": 0.88,
            "registro": {"listasRestrictivas": {"coincidencias": 0}},
        },
    },
}
ALIADOS = ("open-finance", "datos-abiertos")
DEMORAS = {"nominal": 50, "demora150": 150, "lento": 1000}
DEMORA_BASE = 50
EXTRAS_ADICIONAL = {
    "accountNumbers": ["SIN-CTA-0004"],
    "fullName": "Cliente Sintético 04",
    "transactions": [{"amount": "123.00"}],
}
NO_DISPONIBLE = {"error": "unavailable"}
NO_ENCONTRADO = {"error": "customer_not_found"}
ESTADO_RESTABLECIDO = "Restablecido"


def _aplicar_datos(contenedor: dict[str, Any], datos: Mapping[str, Any], quitar: bool) -> None:
    """Reemplaza (o quita, en `incompleta`) claves del nominal; el punto es una ruta anidada."""
    for clave, valor in datos.items():
        *padres, hoja = clave.split(".")
        destino = contenedor
        for padre in padres:
            destino = destino[padre]
        if quitar:
            destino.pop(hoja, None)
        else:
            destino[hoja] = valor


def _cuerpo_aliado(fuente: str, simulacion: Mapping[str, Any]) -> tuple[int, Any]:
    definicion = FUENTES[fuente]
    comportamiento = simulacion["comportamiento"]
    if comportamiento == "caido":
        return 503, NO_DISPONIBLE
    cuerpo = copy.deepcopy(definicion["nominal"])
    contenedor = cuerpo[definicion["contenedor"]]
    if comportamiento == "malformada":
        cuerpo[definicion["contenedor"]] = "no-es-objeto"
        return 200, cuerpo
    if comportamiento == "adicional":
        contenedor.update(copy.deepcopy(EXTRAS_ADICIONAL))
    _aplicar_datos(contenedor, simulacion.get("datos", {}), quitar=comportamiento == "incompleta")
    return 200, cuerpo


def _solicitud_aliado(fuente: str, cliente_id: str) -> dict[str, Any]:
    definicion = FUENTES[fuente]
    return {
        "method": "GET",
        "urlPath": definicion["ruta"].format(id=cliente_id),
        "queryParameters": {k: {"equalTo": v} for k, v in definicion["parametros"].items()},
        "headers": {"X-Api-Key": {"matches": ".+"}},
    }


def _respuesta_aliado(estado: int, archivo: str, demora_ms: int) -> dict[str, Any]:
    return {
        "status": estado,
        "headers": JSON,
        "bodyFileName": archivo,
        "fixedDelayMilliseconds": demora_ms,
    }


def generar_aliados(casos: Iterable[Mapping[str, Any]]) -> Archivos:
    archivos: Archivos = {}
    for caso in casos:
        for fuente, simulacion in caso["simulador"].items():
            definicion = FUENTES[fuente]
            aliado, recurso = definicion["aliado"], definicion["recurso"]
            base = f"{_nombre(caso)}-{recurso}"
            cuerpo_archivo = f"{aliado}/{base}.json"
            estado, cuerpo = _cuerpo_aliado(fuente, simulacion)
            archivos[f"__files/{cuerpo_archivo}"] = cuerpo
            comportamiento = simulacion["comportamiento"]
            nombre = f"{aliado} {caso['id']} {recurso} {comportamiento}"
            solicitud = _solicitud_aliado(fuente, caso["clienteId"])
            if comportamiento == "escenario_recuperacion":
                escenario = f"recuperacion-{caso['clienteId']}"
                archivos[f"mappings/{aliado}/{base}-1-lento.json"] = {
                    "name": f"{nombre} (1.º lento)",
                    "scenarioName": escenario,
                    "requiredScenarioState": "Started",
                    "newScenarioState": ESTADO_RESTABLECIDO,
                    "request": solicitud,
                    "response": _respuesta_aliado(estado, cuerpo_archivo, DEMORAS["lento"]),
                }
                archivos[f"mappings/{aliado}/{base}-2-restablecido.json"] = {
                    "name": f"{nombre} (restablecido)",
                    "scenarioName": escenario,
                    "requiredScenarioState": ESTADO_RESTABLECIDO,
                    "request": solicitud,
                    "response": _respuesta_aliado(estado, cuerpo_archivo, DEMORA_BASE),
                }
                continue
            archivos[f"mappings/{aliado}/{base}.json"] = {
                "name": nombre,
                "request": solicitud,
                "response": _respuesta_aliado(
                    estado, cuerpo_archivo, DEMORAS.get(comportamiento, DEMORA_BASE)
                ),
            }
    for aliado in ALIADOS:
        archivos[f"__files/{aliado}/zz-no-encontrado.json"] = NO_ENCONTRADO
        archivos[f"mappings/{aliado}/zz-no-encontrado.json"] = {
            "name": f"{aliado} ruta no cubierta",
            "priority": PRIORIDAD_RESPALDO,
            "request": {"method": "ANY", "urlPathPattern": f"/{aliado}/.*"},
            "response": {
                "status": 404,
                "headers": JSON,
                "bodyFileName": f"{aliado}/zz-no-encontrado.json",
            },
        }
    return archivos


# --- Escritura ------------------------------------------------------------------------
GENERADORES = {
    "clientes-stub": (generar_clientes_stub, ("mappings/clientes-stub",)),
    "aliados": (
        generar_aliados,
        tuple(f"{raiz}/{aliado}" for raiz in ("mappings", "__files") for aliado in ALIADOS),
    ),
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
