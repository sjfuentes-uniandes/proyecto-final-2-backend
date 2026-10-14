"""N3 HU-W01 contra un ambiente desplegado; configuración en infra/DESPLIEGUE.md."""

import time

import httpx


def test_hu_w01_alta_consumo_rechazo_y_desactivacion_aislada(ambiente) -> None:
    socio_b = ambiente.socio_de_prueba("socio-b")
    config = ambiente.requerir("HU-W01", {
        "admin_token": ambiente.token_admin(), **ambiente.urls,
        "b_client_id": socio_b["client_id"], "b_client_secret": socio_b["client_secret"],
        "b_api_key": socio_b["api_key"],
    })
    admin = {"Authorization": f"Bearer {config['admin_token']}"}
    sello = time.strftime("%Y%m%d%H%M%S")

    alta = httpx.post(f"{config['canales']}/web/socios", headers=admin, timeout=30, json={
        "nombre": f"E2E HU-W01 {sello}", "canal": "plataforma_digital", "plan": "basico", "permisos": ["cotizar"],
    })
    assert alta.status_code == 201, alta.text
    socio = alta.json()
    assert socio["clientSecret"] and socio["apiKey"] and alta.headers.get("cache-control") == "no-store"
    detalle = httpx.get(f"{config['canales']}/web/socios/{socio['id']}", headers=admin, timeout=30)
    assert detalle.status_code == 200 and socio["clientSecret"] not in detalle.text

    try:
        token = ambiente.hasta(lambda: ambiente.token(config["token_url"], socio["clientId"], socio["clientSecret"]), 200)
        assert token.status_code == 200, token.text
        acceso = {"Authorization": f"Bearer {token.json()['access_token']}", "x-api-key": socio["apiKey"]}
        perfil = ambiente.hasta(lambda: httpx.get(f"{config['socios']}/socio/me", headers=acceso, timeout=30), 200)
        assert perfil.status_code == 200, perfil.text
        assert perfil.json()["id"] == socio["id"] and "clientSecret" not in perfil.text
        # AC4
        emitir = ambiente.hasta(lambda: httpx.post(f"{config['socios']}/polizas", headers=acceso, timeout=30), 403)
        assert emitir.status_code == 403 and emitir.json()["codigo"] == "PERMISO_NO_ASIGNADO", emitir.text

        assert ambiente.token(config["token_url"], socio["clientId"], "secreto-incorrecto").status_code in (400, 401)
        invalido = httpx.get(f"{config['socios']}/socio/me", timeout=30,
                             headers={"Authorization": "Bearer token-invalido", "x-api-key": socio["apiKey"]})
        assert invalido.status_code == 401 and invalido.headers.get("x-correlation-id")
    finally:
        desactivado = httpx.post(f"{config['canales']}/web/socios/{socio['id']}/desactivar", headers=admin, timeout=30)
        assert desactivado.status_code == 200 and desactivado.json()["estado"] == "desactivado"

    assert ambiente.token(config["token_url"], socio["clientId"], socio["clientSecret"]).status_code in (400, 401)
    rechazado = ambiente.hasta(lambda: httpx.get(f"{config['socios']}/socio/me", headers=acceso, timeout=30), 401)
    assert rechazado.status_code == 401, rechazado.text

    token_b = ambiente.token(config["token_url"], config["b_client_id"], config["b_client_secret"])
    assert token_b.status_code == 200, token_b.text
    salud_b = httpx.get(f"{config['socios']}/health", timeout=30,
                        headers={"Authorization": f"Bearer {token_b.json()['access_token']}",
                                 "x-api-key": config["b_api_key"]})
    assert salud_b.status_code == 200, salud_b.text
