"""Compuerta de cobertura por servicio (docs/sprint_1/01-acuerdos-tecnicos.md §3.11).

Uso: python scripts/calidad/cobertura.py <servicio> <ruta de cobertura.json>
Termina con 1 si las líneas o las ramas quedan bajo el umbral del servicio.
"""

import json
import sys
from pathlib import Path
from typing import NamedTuple


class Umbral(NamedTuple):
    lineas: float
    ramas: float


# Los servicios que no aparecen tienen umbral 0 hasta que su historia lo defina.
UMBRALES: dict[str, Umbral] = {
    "cotizacion": Umbral(90, 85),
    "catalogo": Umbral(90, 85),
    "adaptador-datos": Umbral(90, 0),
    "bff-web": Umbral(85, 0),
}
SIN_UMBRAL = Umbral(0, 0)


def porcentajes(reporte: dict) -> tuple[float, float]:
    """Porcentaje de líneas y de ramas del JSON de coverage.py (100 si no hay ramas)."""
    totales = reporte["totals"]
    ramas = totales.get("num_branches", 0)
    cubiertas = totales.get("covered_branches", 0)
    return float(totales["percent_covered"]), (100 * cubiertas / ramas) if ramas else 100.0


def evaluar(servicio: str, reporte: dict) -> list[str]:
    """Devuelve los incumplimientos; la lista vacía significa que pasa."""
    umbral = UMBRALES.get(servicio, SIN_UMBRAL)
    lineas, ramas = porcentajes(reporte)
    fallas = []
    if lineas < umbral.lineas:
        fallas.append(f"líneas {lineas:.2f} % < {umbral.lineas:.0f} %")
    if ramas < umbral.ramas:
        fallas.append(f"ramas {ramas:.2f} % < {umbral.ramas:.0f} %")
    return fallas


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("uso: cobertura.py <servicio> <cobertura.json>", file=sys.stderr)
        return 2
    servicio, ruta = argv
    reporte = json.loads(Path(ruta).read_text(encoding="utf-8"))
    lineas, ramas = porcentajes(reporte)
    umbral = UMBRALES.get(servicio, SIN_UMBRAL)
    print(
        f"{servicio}: líneas {lineas:.2f} % (mín. {umbral.lineas:.0f}), "
        f"ramas {ramas:.2f} % (mín. {umbral.ramas:.0f})"
    )
    fallas = evaluar(servicio, reporte)
    for falla in fallas:
        print(f"Cobertura insuficiente en {servicio}: {falla}", file=sys.stderr)
    return 1 if fallas else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
