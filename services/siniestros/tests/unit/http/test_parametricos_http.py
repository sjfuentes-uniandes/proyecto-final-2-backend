"""Rutas del siniestro paramétrico (HU-W22): `POST /v1/siniestros/parametricos` y la lectura por lote
de pagos, con el repositorio en memoria."""

import logging
from collections.abc import Iterator
from copy import deepcopy
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from siniestros.adapters.inbound.http.siniestros import Servicios
from siniestros.application.siniestro.acceso import RegistrarAccesoDenegado
from siniestros.application.siniestro.bandeja import ConsultarResumen, ListarBandeja
from siniestros.application.siniestro.crear_parametrico import CrearParametrico
from siniestros.application.siniestro.decidir import Decidir
from siniestros.application.siniestro.detalle import ConsultarDetalle
from siniestros.application.siniestro.enlace import EmitirEnlace
from siniestros.application.siniestro.pagos_parametricos import ConsultarPagosParametricos
from siniestros.application.siniestro.tomar import TomarSiguiente
from siniestros.application.siniestro.vistas import Plazos
from siniestros.main import crear_app

EVENTO = "bafc2b26-7c88-5ae3-b5ac-26fdf5427a09"
POLIZA = "b2100000-0000-4000-8000-000000000101"
CLIENTE = "00000000-0000-4000-8000-000000000002"
CLAVE = f"{EVENTO}:{POLIZA}"
SISTEMA = {"X-Actor-Id": "pagos-parametricos", "X-Actor-Rol": "SISTEMA_PARAMETRICO"}
ANALISTA = {
    "X-Actor-Id": "a0a0a0a0-0000-4000-8000-000000000001",
    "X-Actor-Rol": "ANALISTA_SINIESTROS",
}
CUERPO: dict[str, Any] = {
    "eventoId": EVENTO,
    "polizaId": POLIZA,
    "clienteId": CLIENTE,
    "poliza": {
        "numero": "250-2026-000101",
        "ramo": "Paramétrico",
        "vigencia": {"desde": "2026-09-01", "hasta": "2027-08-31"},
        "moneda": "COP",
    },
    "evento": {
        "fuente": "IDEAM-SIM",
        "tipoEvento": "LLUVIA_ACUMULADA",
        "valorObservado": "135",
        "umbral": "120",
        "unidad": "mm",
        "zona": "11001",
        "observadoEn": "2026-11-10T14:00:00Z",
        "eventoOrigen": "EVT-2026-0913",
    },
    "regla": "VALOR_IGUAL_O_SUPERIOR_AL_UMBRAL",
    "versionRegla": "1",
    "valor": "1400000.00",
    "moneda": "COP",
}


@pytest.fixture
def cliente(repo, almacen, soportes, reloj, plazos: Plazos, codigos) -> Iterator[TestClient]:  # type: ignore[no-untyped-def]
    app = crear_app()
    app.state.servicios = Servicios(
        bandeja=ListarBandeja(repo, reloj, plazos),
        resumen=ConsultarResumen(repo, reloj, plazos),
        tomar=TomarSiguiente(repo, reloj, plazos),
        detalle=ConsultarDetalle(repo, almacen, soportes, reloj, plazos),
        enlace=EmitirEnlace(repo, almacen, reloj, 300),
        decidir=Decidir(repo, reloj),
        crear_parametrico=CrearParametrico(repo, codigos, reloj, plazos),
        pagos_parametricos=ConsultarPagosParametricos(repo),
        acceso=RegistrarAccesoDenegado(repo, reloj),
        reloj=reloj,
        plazos=plazos,
    )
    yield TestClient(app)


def _crear(cliente: TestClient, cuerpo: dict[str, Any] | None = None, **cabeceras: str) -> Any:
    return cliente.post(
        "/v1/siniestros/parametricos",
        json=cuerpo or CUERPO,
        headers={**SISTEMA, "Idempotency-Key": CLAVE, **cabeceras},
    )


def _error(respuesta: Any) -> dict[str, Any]:
    cuerpo = respuesta.json()
    assert set(cuerpo) == {"codigo", "mensaje", "correlationId", "detalles"}
    return cuerpo  # type: ignore[no-any-return]


# --- POST ---------------------------------------------------------------------------------------
def test_crea_el_siniestro_con_201_y_la_repeticion_responde_200_con_el_mismo_id(
    cliente: TestClient,
    repo,  # type: ignore[no-untyped-def]
) -> None:
    primero = _crear(cliente)
    segundo = _crear(cliente)

    assert (primero.status_code, segundo.status_code) == (201, 200)
    assert segundo.json() == primero.json()
    cuerpo = primero.json()
    assert (cuerpo["origen"], cuerpo["estado"], cuerpo["analistaId"]) == (
        "PARAMETRICO",
        "APROBADO",
        None,
    )
    assert (cuerpo["polizaId"], cuerpo["clienteId"], cuerpo["polizaNumero"]) == (
        POLIZA,
        CLIENTE,
        "250-2026-000101",
    )
    assert len(repo.siniestros) == 1


def test_el_detalle_del_paramétrico_muestra_el_evento_y_la_regla_aplicada(
    cliente: TestClient,
) -> None:
    creado = _crear(cliente).json()

    detalle = cliente.get(f"/v1/siniestros/{creado['id']}", headers=ANALISTA).json()

    assert detalle["evento"] == {
        "eventoId": EVENTO,
        "fuente": "IDEAM-SIM",
        "tipoEvento": "LLUVIA_ACUMULADA",
        "valorObservado": "135",
        "umbral": "120",
        "unidad": "mm",
        "zona": "11001",
        "observadoEn": "2026-11-10T14:00:00Z",
        "eventoOrigen": "EVT-2026-0913",
        "regla": "VALOR_IGUAL_O_SUPERIOR_AL_UMBRAL",
        "versionRegla": "1",
    }
    assert detalle["causa"] == "LLUVIA_ACUMULADA"
    assert detalle["pago"]["estado"] == "PENDIENTE"
    assert [h["accion"] for h in detalle["historial"]] == ["CREADO_AUTOMATICAMENTE"]
    assert detalle["historial"][0]["actorRol"] == "SISTEMA_PARAMETRICO"
    assert detalle["historial"][0]["actorId"] is None
    assert [(p["paso"], p["estado"]) for p in detalle["tramite"]][-1] == (
        "PAGO_INDEMNIZACION",
        "ACTUAL",
    )
    assert detalle["poliza"]["beneficiario"]["beneficiarioRef"] == f"ben-par-{CLIENTE}"


def test_un_siniestro_de_otro_origen_no_trae_evento(
    cliente: TestClient,
    repo,
    siniestro,  # type: ignore[no-untyped-def]
) -> None:
    caso = siniestro()
    repo.agregar(caso)

    detalle = cliente.get(f"/v1/siniestros/{caso.id}", headers=ANALISTA).json()

    assert detalle["evento"] is None


def test_sin_actor_es_401(cliente: TestClient) -> None:
    respuesta = cliente.post(
        "/v1/siniestros/parametricos", json=CUERPO, headers={"Idempotency-Key": CLAVE}
    )
    assert (respuesta.status_code, _error(respuesta)["codigo"]) == (401, "ACTOR_REQUERIDO")


@pytest.mark.parametrize(
    ("rol", "motivo"),
    [(None, "ROL_AUSENTE"), ("ANALISTA_SINIESTROS", "ROL_INVALIDO"), ("x" * 90, "ROL_INVALIDO")],
)
def test_otro_rol_es_403_y_se_audita_sin_crear_nada(
    cliente: TestClient,
    repo,
    rol: str | None,
    motivo: str,  # type: ignore[no-untyped-def]
) -> None:
    cabeceras = {"X-Actor-Id": "pagos-parametricos", "Idempotency-Key": CLAVE}
    if rol:
        cabeceras["X-Actor-Rol"] = rol

    respuesta = cliente.post("/v1/siniestros/parametricos", json=CUERPO, headers=cabeceras)

    assert (respuesta.status_code, _error(respuesta)["codigo"]) == (403, "ROL_NO_AUTORIZADO")
    assert not repo.siniestros
    ((tipo, acceso),) = repo.eventos
    assert tipo == "AccesoSiniestroDenegado"
    assert (acceso.recurso.value, acceso.motivo.value, acceso.origen.value) == (
        "SINIESTRO",
        motivo,
        "SINIESTROS",
    )


def test_la_misma_clave_con_otro_cuerpo_es_422_idempotencia_conflicto(cliente: TestClient) -> None:
    _crear(cliente)
    otro = deepcopy(CUERPO) | {"valor": "620000.00"}

    respuesta = _crear(cliente, otro)

    assert (respuesta.status_code, _error(respuesta)["codigo"]) == (422, "IDEMPOTENCIA_CONFLICTO")


@pytest.mark.parametrize(
    "clave",
    [f"{uuid4()}:{POLIZA}", f"{EVENTO}:{uuid4()}", f"{POLIZA}:{EVENTO}"],
)
def test_una_clave_que_no_es_la_pareja_del_cuerpo_es_422(cliente: TestClient, clave: str) -> None:
    respuesta = _crear(cliente, **{"Idempotency-Key": clave})

    assert (respuesta.status_code, _error(respuesta)["codigo"]) == (422, "SOLICITUD_INVALIDA")
    assert _error(respuesta)["detalles"] == {"campos": ["Idempotency-Key"]}


@pytest.mark.parametrize("clave", ["", "no-es-una-clave", str(uuid4()), f"{EVENTO}:"])
def test_una_clave_con_otra_forma_es_422(cliente: TestClient, clave: str) -> None:
    respuesta = _crear(cliente, **{"Idempotency-Key": clave})

    assert (respuesta.status_code, _error(respuesta)["codigo"]) == (422, "SOLICITUD_INVALIDA")


def test_sin_clave_es_422(cliente: TestClient) -> None:
    respuesta = cliente.post("/v1/siniestros/parametricos", json=CUERPO, headers=SISTEMA)

    assert respuesta.status_code == 422


@pytest.mark.parametrize(
    ("cambio", "campo"),
    [
        ({"moneda": "USD"}, "moneda"),
        ({"valor": "0.00"}, "valor"),
        (
            {
                "poliza": {
                    **CUERPO["poliza"],
                    "vigencia": {"desde": "2026-13-45", "hasta": "2027-01-01"},
                }
            },
            "poliza.vigencia",
        ),
        (
            {
                "poliza": {
                    **CUERPO["poliza"],
                    "vigencia": {"desde": "2027-09-01", "hasta": "2027-08-31"},
                }
            },
            "poliza.vigencia",
        ),
    ],
)
def test_un_cuerpo_incoherente_es_422_con_su_campo(
    cliente: TestClient,
    repo,
    cambio: dict[str, Any],
    campo: str,  # type: ignore[no-untyped-def]
) -> None:
    respuesta = _crear(cliente, deepcopy(CUERPO) | cambio)

    assert (respuesta.status_code, _error(respuesta)["codigo"]) == (422, "SOLICITUD_INVALIDA")
    assert campo in _error(respuesta)["detalles"]["campos"]
    assert not repo.siniestros


@pytest.mark.parametrize(
    "cambio",
    [
        {"valor": "-1"},
        {"valor": "1.234"},
        {"extra": "campo de más"},
        {"eventoId": "no-es-uuid"},
        {"regla": "minusculas"},
        {"evento": {**CUERPO["evento"], "zona": "bogota"}},
        {"evento": {**CUERPO["evento"], "valorObservado": "1,5"}},
    ],
)
def test_un_cuerpo_fuera_de_formato_es_422(cliente: TestClient, cambio: dict[str, Any]) -> None:
    respuesta = _crear(cliente, deepcopy(CUERPO) | cambio)

    assert (respuesta.status_code, _error(respuesta)["codigo"]) == (422, "SOLICITUD_INVALIDA")


def test_una_medicion_sin_zona_horaria_es_422(cliente: TestClient) -> None:
    cuerpo = deepcopy(CUERPO)
    cuerpo["evento"]["observadoEn"] = "2026-11-10T14:00:00"

    respuesta = _crear(cliente, cuerpo)

    assert (respuesta.status_code, _error(respuesta)["codigo"]) == (422, "SOLICITUD_INVALIDA")


def test_el_log_de_cierre_no_lleva_la_medicion_ni_al_cliente(
    cliente: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    _crear(cliente)
    _crear(cliente)

    cierres = [r for r in caplog.records if r.getMessage().startswith("siniestro: parametrico")]
    assert [r.getMessage() for r in cierres] == [
        "siniestro: parametrico creado",
        "siniestro: parametrico existente",
    ]
    assert cierres[0].__dict__["operacion"] == "siniestro.crear_parametrico"
    for centinela in ("IDEAM-SIM", "135", CLIENTE, "ben-par", "1400000"):
        assert centinela not in caplog.text


# --- GET pagos ----------------------------------------------------------------------------------
def _pagos(cliente: TestClient, ids: list[Any], **cabeceras: str) -> Any:
    return cliente.get(
        "/v1/siniestros/parametricos/pagos",
        params={"siniestroIds": [str(i) for i in ids]},
        headers=cabeceras or ANALISTA,
    )


def test_la_lectura_por_lote_devuelve_el_estado_de_cada_paramétrico(cliente: TestClient) -> None:
    creado = _crear(cliente).json()

    respuesta = _pagos(cliente, [creado["id"], uuid4()])

    assert respuesta.status_code == 200
    assert respuesta.json() == {
        "items": [
            {"siniestroId": creado["id"], "codigo": creado["codigo"], "estadoPago": "PENDIENTE"}
        ]
    }


@pytest.mark.parametrize("cuantos", [0, 21])
def test_la_lectura_por_lote_admite_de_1_a_20_ids(cliente: TestClient, cuantos: int) -> None:
    respuesta = _pagos(cliente, [uuid4() for _ in range(cuantos)])

    assert (respuesta.status_code, _error(respuesta)["codigo"]) == (422, "SOLICITUD_INVALIDA")


def test_un_id_que_no_es_uuid_es_422(cliente: TestClient) -> None:
    assert _pagos(cliente, ["no-es-uuid"]).status_code == 422


def test_la_lectura_por_lote_pide_el_rol_de_analista_y_audita_al_resto(
    cliente: TestClient,
    repo,  # type: ignore[no-untyped-def]
) -> None:
    respuesta = _pagos(cliente, [uuid4()], **SISTEMA)

    assert (respuesta.status_code, _error(respuesta)["codigo"]) == (403, "ROL_NO_AUTORIZADO")
    ((tipo, _),) = repo.eventos
    assert tipo == "AccesoSiniestroDenegado"


def test_la_lectura_por_lote_sin_actor_es_401(cliente: TestClient) -> None:
    respuesta = cliente.get(
        "/v1/siniestros/parametricos/pagos", params={"siniestroIds": [str(uuid4())]}
    )

    assert (respuesta.status_code, _error(respuesta)["codigo"]) == (401, "ACTOR_REQUERIDO")


def test_la_lectura_por_lote_no_se_confunde_con_el_detalle(cliente: TestClient) -> None:
    # `parametricos` nunca se lee como un siniestroId: la ruta fija va antes que `/{siniestroId}`.
    respuesta = cliente.get("/v1/siniestros/parametricos/pagos", headers=ANALISTA)

    assert respuesta.status_code == 422  # le falta `siniestroIds`, no es un 422 de UUID de `{id}`
    assert "siniestroIds" in respuesta.text


def test_el_valor_se_guarda_como_texto_decimal_con_dos_decimales(
    cliente: TestClient,
    repo,  # type: ignore[no-untyped-def]
) -> None:
    _crear(cliente)

    (s,) = repo.siniestros.values()
    assert s.valor_indemnizar == Decimal("1400000.00")
