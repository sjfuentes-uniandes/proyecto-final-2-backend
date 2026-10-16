"""N3 HU-W28 contra un ambiente desplegado: pantalla Operación › Alertas (bff-web).

La activación de la alarma con una métrica sintética (N2) es make infra-probar-alerta.
"""

import httpx


def test_hu_w28_panel_de_alertas_del_ambiente(ambiente) -> None:
    config = ambiente.requerir("HU-W28", {"admin_token": ambiente.token_admin(), "canales": ambiente.urls["canales"]})
    url = f"{config['canales']}/web/operacion/alertas"

    assert httpx.get(url, timeout=30).status_code == 401

    respuesta = httpx.get(url, headers={"Authorization": f"Bearer {config['admin_token']}"}, timeout=30)
    assert respuesta.status_code != 403, "El usuario E2E debe estar en el grupo operacion (infra/DESPLIEGUE.md, HU-W28)"
    assert respuesta.status_code == 200, respuesta.text
    panel = respuesta.json()

    perfilamiento = next(u for u in panel["umbrales"] if u["metrica"] == "Perfilamiento p95")
    assert (perfilamiento["meta"], perfilamiento["critica"]) == (400, 400)
    assert perfilamiento["advertencia"] == 400 * panel["porcentaje_advertencia"] / 100
    assert {"Tasa de error técnico", "Cuota de socio", "Disponibilidad"} <= {u["metrica"] for u in panel["umbrales"]}

    for alerta in panel["abiertas"]:
        assert alerta["alarma"].startswith(f"solventa-{panel['ambiente']}-")
        assert alerta["severidad"] in {"critica", "advertencia", "informativa"}
        assert alerta["enlace"]["tipo"] in {"traza", "socio", "cola", "tablero"}

    resumen = panel["resumen"]
    assert resumen["generadas"] >= resumen["criticas"] >= 0
    assert 0 <= resumen["metas_incumplidas"] <= resumen["criticas"]
