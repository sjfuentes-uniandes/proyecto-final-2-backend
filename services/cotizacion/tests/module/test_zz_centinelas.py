"""Corre al final de la suite de módulo (orden alfabético): ningún valor sensible de 01 §3.9
apareció en los logs ni en los spans de cotizacion, catalogo ni adaptador-datos."""

from typing import Any


def test_zz_centinelas_ausentes_en_logs_y_spans(registro: Any, centinelas: tuple[str, ...]) -> None:
    assert registro.logs, "la suite no capturó logs"
    nombres = {s.name for e in registro.spans for s in e.get_finished_spans()}
    assert "perfilamiento" in nombres
    assert any(n.startswith("consulta.") for n in nombres), "faltan los spans del adaptador"
    texto = registro.texto()
    for centinela in centinelas:
        assert centinela not in texto, centinela
