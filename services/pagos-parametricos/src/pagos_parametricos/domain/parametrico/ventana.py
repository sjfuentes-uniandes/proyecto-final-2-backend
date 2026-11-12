"""La ventana de una cobertura es la `vigencia` de la póliza (HU-W21, R-150).

`PolizaEmitida.vigencia` son fechas **inclusivas** en America/Bogota: `vigente_desde` es el día
`desde` a las 00:00 de Colombia y `vigente_hasta`, el día **siguiente** a `hasta` a las 00:00 de
Colombia, en UTC con el desfase fijo de UTC−5 (R-20; sin horario de verano). La ventana es
`[vigente_desde, vigente_hasta)`.
"""

from datetime import UTC, date, datetime, time, timedelta, timezone

COLOMBIA = timezone(timedelta(hours=-5))


def ventana_de_vigencia(desde: date, hasta: date) -> tuple[datetime, datetime]:
    """`[inicio, fin)` en UTC; `hasta` debe ser igual o posterior a `desde`."""
    if hasta < desde:
        raise ValueError("la vigencia termina antes de empezar")
    inicio = datetime.combine(desde, time.min, tzinfo=COLOMBIA)
    fin = datetime.combine(hasta + timedelta(days=1), time.min, tzinfo=COLOMBIA)
    return inicio.astimezone(UTC), fin.astimezone(UTC)
