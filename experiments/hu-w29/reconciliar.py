"""Reconciliación del experimento multizona (HU-W29, AC2): ninguna operación confirmada se
pierde ni se duplica, y ningún mensaje queda en DLQ sin explicación.

Entradas (en resultados/):
- carga.log: consola de k6 (carga.js); cada línea "hu29" es un intento con su clave idempotente.
- observadas.ndjson: filas de las bases en la ventana (extraer.sh).
- dlq.ndjson: mensajes de las DLQ (recolectar.sh).
- explicaciones.json (opcional): {"<messageId>": "<motivo>"} para los mensajes de DLQ revisados.

Salida: informe.json (versión 1), que publicar.sh sube al bucket de experimentos y que bff-web
muestra en Operación › Conciliación (I05). Sale con 0 solo si no hay diferencias ni pendientes.

    uv run python experiments/hu-w29/reconciliar.py --experimento EXP123 \
        --desde 2026-11-20T14:00:00Z --hasta 2026-11-20T14:30:00Z

Sin dependencias fuera de la biblioteca estándar: corre en la máquina de quien ejecuta el
experimento. R5 (eventos firmados) queda como verificación pendiente hasta HU-W21 y HU-W22.
"""

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

VERSION = 1
COINCIDE, DIFERENCIA, PENDIENTE = "COINCIDE", "DIFERENCIA", "PENDIENTE"
# Paso de carga.js → tipo de fila de extraer.sh donde debe existir una vez por clave.
DESTINOS = {"cotizacion": "cotizacion", "aceptacion": "aceptacion"}
# Objetivo de recuperación de ARQ-001 (manifiesto.md): la carga vuelve a una tasa de error
# menor o igual a error_rate_percent (1 %, HU-W28) en 10 minutos o menos desde la inyección.
OBJETIVO_REDIRECCION_S = 600
UMBRAL_ERROR_PCT = 1.0
_MSG_K6 = re.compile(r'msg="((?:[^"\\]|\\.)*)"')

Fila = Mapping[str, Any]


def leer_intentos(lineas: Iterable[str]) -> list[dict[str, Any]]:
    """Intentos de carga.js: líneas JSON sueltas o en el formato logfmt de --console-output."""
    intentos = []
    for linea in lineas:
        texto = linea.strip()
        if '"hu29"' not in texto and '\\"hu29\\"' not in texto:
            continue
        if not texto.startswith("{"):
            encontrado = _MSG_K6.search(texto)
            if not encontrado:
                continue
            # logfmt escapa como una cadena JSON (\" y \\).
            texto = json.loads(f'"{encontrado.group(1)}"')
        try:
            intento = json.loads(texto)
        except json.JSONDecodeError:
            continue
        if intento.get("hu29") == 1:
            intentos.append(intento)
    return intentos


def leer_ndjson(lineas: Iterable[str]) -> list[dict[str, Any]]:
    return [json.loads(linea) for linea in lineas if linea.strip()]


def _verificacion(
    id_: str, descripcion: str, esperado: int, observado: int, resultado: str, detalle: list[Any]
) -> dict[str, Any]:
    return {
        "id": id_,
        "descripcion": descripcion,
        "esperado": esperado,
        "observado": observado,
        "resultado": resultado,
        "detalle": detalle[:50],  # suficiente para investigar sin inflar el informe
    }


def _existencia_unica(
    id_: str, descripcion: str, claves: set[str], filas: list[Fila]
) -> dict[str, Any]:
    """Cada clave confirmada debe tener exactamente una fila."""
    por_clave: dict[str, list[Fila]] = {}
    for fila in filas:
        por_clave.setdefault(str(fila["clave"]), []).append(fila)
    faltantes = sorted(c for c in claves if c not in por_clave)
    duplicadas = sorted(c for c in claves if len(por_clave.get(c, [])) > 1)
    unicas = len(claves) - len(faltantes) - len(duplicadas)
    resultado = COINCIDE if not faltantes and not duplicadas else DIFERENCIA
    detalle = [{"clave": c, "problema": "FALTANTE"} for c in faltantes] + [
        {"clave": c, "problema": "DUPLICADA", "filas": len(por_clave[c])} for c in duplicadas
    ]
    return _verificacion(id_, descripcion, len(claves), unicas, resultado, detalle)


def _recorridos(intentos: list[dict[str, Any]]) -> dict[str, Any]:
    resumen: dict[str, Any] = {}
    for recorrido in ("R1", "R3"):
        propios = [i for i in intentos if i.get("recorrido") == recorrido]
        errores = [i for i in propios if not i.get("confirmada")]
        resumen[recorrido] = {
            "intentos": len(propios),
            "confirmadas": len({(i["paso"], i["clave"]) for i in propios if i.get("confirmada")}),
            "errores": len(errores),
            "erroresPorEstado": dict(
                sorted(Counter(str(i.get("status")) for i in errores).items())
            ),
        }
    if any(i.get("recorrido") == "R5" for i in intentos):
        propios = [i for i in intentos if i.get("recorrido") == "R5"]
        resumen["R5"] = {
            "intentos": len(propios),
            "confirmadas": len({i["clave"] for i in propios if i.get("confirmada")}),
            "errores": sum(1 for i in propios if not i.get("confirmada")),
            "estado": "SIN_RECONCILIAR",
        }
    else:
        resumen["R5"] = {"estado": "PENDIENTE_HU_W21_W22"}
    return resumen


def _instante(texto: str) -> datetime:
    return datetime.fromisoformat(texto)


def continuidad(
    intentos: list[dict[str, Any]],
    inyeccion: str,
    objetivo_s: int = OBJETIVO_REDIRECCION_S,
    umbral_pct: float = UMBRAL_ERROR_PCT,
) -> dict[str, Any]:
    """AC1 y AC3: serie de errores por minuto y tiempo hasta que la carga se estabiliza.

    La redirección termina al inicio del primer minuto, desde la inyección, a partir del cual
    todos los minutos siguientes tienen una tasa de error menor o igual al umbral.
    """
    inicio = _instante(inyeccion)
    por_minuto: dict[datetime, list[bool]] = {}
    for intento in intentos:
        if intento.get("instante"):
            minuto = _instante(str(intento["instante"])).replace(second=0, microsecond=0)
            por_minuto.setdefault(minuto, []).append(bool(intento.get("confirmada")))
    serie: list[dict[str, Any]] = [
        {
            "minuto": m.strftime("%Y-%m-%dT%H:%MZ"),
            "solicitudes": len(r),
            "errores": r.count(False),
            "porcentajeError": round(100 * r.count(False) / len(r), 2),
        }
        for m, r in sorted(por_minuto.items())
    ]
    despues = [
        (m, p)
        for m, p in zip(sorted(por_minuto), serie, strict=True)
        if m >= inicio.replace(second=0, microsecond=0)
    ]
    estable: datetime | None = None
    for minuto, punto in reversed(despues):
        if punto["porcentajeError"] > umbral_pct:
            break
        estable = minuto
    redireccion = None if estable is None else max(0, int((estable - inicio).total_seconds()))
    return {
        "inyeccion": inyeccion,
        "umbralErrorPorcentaje": umbral_pct,
        "objetivoSegundos": objetivo_s,
        "redireccionSegundos": redireccion,
        "cumpleObjetivo": redireccion is not None and redireccion <= objetivo_s,
        "serie": serie,
    }


def _confirmadas(intentos: list[dict[str, Any]]) -> tuple[dict[str, set[str]], set[str]]:
    """Claves confirmadas (2xx) por paso, e ids de las aceptaciones confirmadas."""
    claves: dict[str, set[str]] = {paso: set() for paso in DESTINOS}
    aceptaciones: set[str] = set()
    for intento in intentos:
        paso = intento.get("paso")
        if not intento.get("confirmada") or paso not in DESTINOS:
            continue
        claves[paso].add(str(intento["clave"]))
        if paso == "aceptacion" and intento.get("id"):
            aceptaciones.add(str(intento["id"]))
    return claves, aceptaciones


def _cobros(
    aceptaciones: set[str], recaudos: Counter[str], en_transito: bool
) -> list[dict[str, Any]]:
    """El cobro: dinero lo crea al consumir OfertaAceptada, con claveRecaudo = aceptacionId."""
    sin_cobro = sorted(i for i in aceptaciones if recaudos[i] == 0)
    if not sin_cobro:
        resultado = COINCIDE
    elif en_transito:
        resultado = PENDIENTE  # el evento sigue en el Outbox o en su DLQ: aún puede llegar
    else:
        resultado = DIFERENCIA
    duplicados = sorted(clave for clave, n in recaudos.items() if n > 1)
    return [
        _verificacion(
            "cobro_por_aceptacion",
            "Cada aceptación confirmada tiene un cobro",
            len(aceptaciones),
            sum(1 for i in aceptaciones if recaudos[i] == 1),
            resultado,
            [{"aceptacionId": i, "problema": "SIN_COBRO"} for i in sin_cobro],
        ),
        _verificacion(
            "cobros_duplicados",
            "Cobros duplicados por reintento",
            0,
            len(duplicados),
            DIFERENCIA if duplicados else COINCIDE,
            [{"claveRecaudo": c, "cobros": recaudos[c]} for c in duplicados],
        ),
    ]


def _estado(verificaciones: list[dict[str, Any]]) -> str:
    resultados = {v["resultado"] for v in verificaciones}
    if DIFERENCIA in resultados:
        return "CON_DIFERENCIAS"
    if PENDIENTE in resultados:
        return "PENDIENTE"
    return "SIN_DIFERENCIAS"


def _contar(verificaciones: list[dict[str, Any]], problema: str) -> int:
    return sum(1 for v in verificaciones for d in v["detalle"] if d.get("problema") == problema)


def conciliar(
    intentos: list[dict[str, Any]],
    observadas: list[dict[str, Any]],
    dlq: list[dict[str, Any]],
    explicaciones: Mapping[str, str],
    experimento: str,
    desde: str,
    hasta: str,
    ahora: datetime | None = None,
    inyeccion: str | None = None,
) -> dict[str, Any]:
    claves, aceptaciones = _confirmadas(intentos)

    def filas(tipo: str) -> list[Fila]:
        return [f for f in observadas if f.get("tipo") == tipo]

    existencia = [
        _existencia_unica(
            "cotizaciones",
            "Cotizaciones confirmadas (R1 y R3) registradas una vez",
            claves["cotizacion"],
            filas("cotizacion"),
        ),
        _existencia_unica(
            "aceptaciones",
            "Aceptaciones confirmadas (R3) registradas una vez",
            claves["aceptacion"],
            filas("aceptacion"),
        ),
    ]
    recaudos = Counter(str(f["clave"]) for f in filas("recaudo"))
    pendientes = {str(f["base"]): int(f["total"]) for f in filas("outbox_pendiente")}
    en_transito = pendientes.get("polizas", 0) > 0 or any(
        str(d.get("cola", "")).endswith("dinero-aceptaciones-dlq") for d in dlq
    )
    sin_explicacion = [d for d in dlq if str(d.get("messageId")) not in explicaciones]
    total_pendientes = sum(pendientes.values())
    verificaciones = [
        *existencia,
        *_cobros(aceptaciones, recaudos, en_transito),
        _verificacion(
            "dlq_sin_explicacion",
            "Mensajes en DLQ sin explicación",
            0,
            len(sin_explicacion),
            DIFERENCIA if sin_explicacion else COINCIDE,
            [{"cola": d.get("cola"), "messageId": d.get("messageId")} for d in sin_explicacion],
        ),
        _verificacion(
            "outbox_pendiente",
            "Eventos pendientes de entrega (Outbox)",
            0,
            total_pendientes,
            PENDIENTE if total_pendientes else COINCIDE,
            [{"base": b, "pendientes": n} for b, n in sorted(pendientes.items()) if n],
        ),
    ]
    errores = sum(1 for i in intentos if not i.get("confirmada"))
    return {
        "version": VERSION,
        "experimento": experimento,
        "generado": (ahora or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ventana": {"desde": desde, "hasta": hasta},
        "estado": _estado(verificaciones),
        "resumen": {
            "confirmadas": len(claves["cotizacion"]) + len(claves["aceptacion"]),
            "perdidas": _contar(existencia, "FALTANTE"),
            "duplicadas": _contar(existencia, "DUPLICADA"),
            "cobrosDuplicados": verificaciones[3]["observado"],
            "dlqSinExplicacion": len(sin_explicacion),
            "pendientesOutbox": total_pendientes,
            "solicitudes": len(intentos),
            "solicitudesConError": errores,
            "porcentajeError": round(100 * errores / len(intentos), 2) if intentos else 0.0,
        },
        "verificaciones": verificaciones,
        "recorridos": _recorridos(intentos),
        "continuidad": continuidad(intentos, inyeccion) if inyeccion else None,
        "dlq": {
            "total": len(dlq),
            "explicados": [
                {
                    "cola": d.get("cola"),
                    "messageId": d.get("messageId"),
                    "motivo": explicaciones[str(d.get("messageId"))],
                }
                for d in dlq
                if str(d.get("messageId")) in explicaciones
            ],
        },
    }


def _leer(ruta: Path) -> list[str]:
    return ruta.read_text(encoding="utf-8").splitlines() if ruta.exists() else []


def main(argv: list[str] | None = None) -> int:
    raiz = Path(__file__).parent / "resultados"
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--experimento", required=True, help="id del experimento FIS (resultados/experimento.txt)"
    )
    parser.add_argument("--desde", required=True)
    parser.add_argument("--hasta", required=True)
    parser.add_argument("--resultados", type=Path, default=raiz)
    parser.add_argument("--inyeccion", help="inicio de la acción de FIS (fis-experimento.json)")
    args = parser.parse_args(argv)

    carpeta: Path = args.resultados
    explicaciones_ruta = carpeta / "explicaciones.json"
    informe = conciliar(
        leer_intentos(_leer(carpeta / "carga.log")),
        leer_ndjson(_leer(carpeta / "observadas.ndjson")),
        leer_ndjson(_leer(carpeta / "dlq.ndjson")),
        json.loads(explicaciones_ruta.read_text(encoding="utf-8"))
        if explicaciones_ruta.exists()
        else {},
        args.experimento,
        args.desde,
        args.hasta,
        inyeccion=args.inyeccion,
    )
    (carpeta / "informe.json").write_text(
        json.dumps(informe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    resumen = informe["resumen"]
    print(
        f"{informe['estado']}: {resumen['confirmadas']} confirmadas, {resumen['perdidas']} perdidas, "
        f"{resumen['duplicadas']} duplicadas, {resumen['cobrosDuplicados']} cobros duplicados, "
        f"{resumen['dlqSinExplicacion']} en DLQ sin explicación → {carpeta / 'informe.json'}"
    )
    return 0 if informe["estado"] == "SIN_DIFERENCIAS" else 1


if __name__ == "__main__":
    sys.exit(main())
