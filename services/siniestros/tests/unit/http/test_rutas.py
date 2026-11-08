"""Rutas HTTP de siniestros con casos de uso reales sobre un repositorio en memoria (HU-W19)."""

import json
import logging
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from siniestros.adapters.inbound.http.siniestros import Servicios
from siniestros.application.siniestro.acceso import RegistrarAccesoDenegado
from siniestros.application.siniestro.bandeja import ConsultarResumen, ListarBandeja
from siniestros.application.siniestro.detalle import ConsultarDetalle
from siniestros.application.siniestro.enlace import EmitirEnlace
from siniestros.application.siniestro.tomar import TomarSiguiente
from siniestros.application.siniestro.vistas import Indicadores, PaginaSiniestros, Plazos
from siniestros.domain.siniestro.modelos import (
    EstadoSiniestro,
    IntegridadEvidencia,
    Siniestro,
)
from siniestros.main import crear_app

ANALISTA = "a0a0a0a0-0000-4000-8000-000000000001"
CABECERAS = {
    "X-Actor-Id": ANALISTA,
    "X-Actor-Rol": "ANALISTA_SINIESTROS",
    "X-Actor-Nombre": "Luis Mora",
}


@pytest.fixture
def cliente(repo, almacen, soportes, reloj, plazos: Plazos) -> Iterator[TestClient]:  # type: ignore[no-untyped-def]
    app = crear_app()
    app.state.servicios = Servicios(
        bandeja=ListarBandeja(repo, reloj, plazos),
        resumen=ConsultarResumen(repo, reloj, plazos),
        tomar=TomarSiguiente(repo, reloj, plazos),
        detalle=ConsultarDetalle(repo, almacen, soportes, reloj, plazos),
        enlace=EmitirEnlace(repo, almacen, reloj, 300),
        acceso=RegistrarAccesoDenegado(repo, reloj),
        reloj=reloj,
        plazos=plazos,
    )
    yield TestClient(app)


def error_b2(respuesta) -> dict:  # type: ignore[no-untyped-def]
    cuerpo = respuesta.json()
    assert set(cuerpo) == {"codigo", "mensaje", "correlationId", "detalles"}
    return cuerpo


# --- Actor y rol -----------------------------------------------------------------------------------
RUTAS = [
    ("GET", "/v1/siniestros"),
    ("GET", "/v1/siniestros/resumen?desde=2026-11-05T05:00:00Z&hasta=2026-11-06T05:00:00Z"),
    ("POST", "/v1/siniestros/siguiente"),
    ("GET", f"/v1/siniestros/{uuid4()}"),
    ("POST", f"/v1/siniestros/{uuid4()}/evidencias/{uuid4()}/enlace"),
]


@pytest.mark.parametrize(("metodo", "ruta"), RUTAS)
def test_sin_actor_es_401(cliente: TestClient, metodo: str, ruta: str) -> None:
    respuesta = cliente.request(metodo, ruta, headers={"Idempotency-Key": str(uuid4())})
    assert respuesta.status_code == 401
    assert error_b2(respuesta)["codigo"] == "ACTOR_REQUERIDO"


@pytest.mark.parametrize("actor", ["", "   ", "x" * 129])
def test_un_actor_vacio_o_demasiado_largo_es_401(cliente: TestClient, actor: str) -> None:
    respuesta = cliente.get("/v1/siniestros", headers={**CABECERAS, "X-Actor-Id": actor})
    assert respuesta.status_code == 401


def test_un_actor_que_no_es_uuid_es_401(cliente: TestClient) -> None:
    respuesta = cliente.get("/v1/siniestros", headers={**CABECERAS, "X-Actor-Id": "no-es-uuid"})
    assert (respuesta.status_code, error_b2(respuesta)["codigo"]) == (401, "ACTOR_REQUERIDO")


@pytest.mark.parametrize(("metodo", "ruta"), RUTAS)
@pytest.mark.parametrize(
    ("rol", "motivo"),
    [(None, "ROL_AUSENTE"), ("CLIENTE", "ROL_INVALIDO"), ("x" * 90, "ROL_INVALIDO")],
)
def test_un_rol_invalido_es_403_y_se_audita_antes_de_responder(
    cliente: TestClient,
    repo,
    metodo: str,
    ruta: str,
    rol: str | None,
    motivo: str,  # type: ignore[no-untyped-def]
) -> None:
    cabeceras = {"X-Actor-Id": ANALISTA, "Idempotency-Key": str(uuid4())}
    if rol:
        cabeceras["X-Actor-Rol"] = rol
    respuesta = cliente.request(metodo, ruta, headers=cabeceras)
    assert respuesta.status_code == 403
    assert error_b2(respuesta)["codigo"] == "ROL_NO_AUTORIZADO"
    ((_, acceso),) = repo.eventos
    assert (acceso.actor_id, acceso.motivo.value, acceso.origen.value) == (
        ANALISTA,
        motivo,
        "SINIESTROS",
    )
    assert acceso.rol == (rol[:64] if rol else None)


@pytest.mark.parametrize(
    ("ruta", "recurso"),
    [
        ("/v1/siniestros", "BANDEJA"),
        ("/v1/siniestros/resumen?desde=2026-11-05T05:00:00Z&hasta=2026-11-06T05:00:00Z", "RESUMEN"),
    ],
)
def test_el_recurso_denegado_queda_en_el_evento(
    cliente: TestClient,
    repo,
    ruta: str,
    recurso: str,  # type: ignore[no-untyped-def]
) -> None:
    cliente.get(ruta, headers={"X-Actor-Id": ANALISTA, "X-Actor-Rol": "CLIENTE"})
    assert repo.eventos[0][1].recurso.value == recurso


def test_el_id_del_siniestro_denegado_queda_en_el_evento(cliente: TestClient, repo) -> None:  # type: ignore[no-untyped-def]
    identificador = uuid4()
    cliente.get(f"/v1/siniestros/{identificador}", headers={"X-Actor-Id": ANALISTA})
    acceso = repo.eventos[0][1]
    assert (acceso.recurso.value, acceso.recurso_id) == ("SINIESTRO", str(identificador))


# --- Bandeja y resumen -----------------------------------------------------------------------------
def test_bandeja_en_camel_case_con_el_filtro_del_actor(
    cliente: TestClient,
    repo,
    siniestro: Callable[..., Siniestro],
    ahora: datetime,  # type: ignore[no-untyped-def]
) -> None:
    caso = siniestro()
    repo.pagina = PaginaSiniestros((caso,), total=7, siguiente_cursor="sig")
    respuesta = cliente.get(
        "/v1/siniestros",
        params={"bandeja": "SIN_ASIGNAR", "q": "0442", "limite": 5, "prioridad": "BAJA"},
        headers=CABECERAS,
    )
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert (cuerpo["total"], cuerpo["siguienteCursor"]) == (7, "sig")
    (item,) = cuerpo["items"]
    assert item == {
        "id": str(caso.id),
        "codigo": "88-2026-0442",
        "polizaId": str(caso.poliza_id),
        "polizaNumero": "220-2026-011294",
        "clienteId": str(caso.cliente_id),
        "ramo": "Dispositivos",
        "origen": "MOVIL",
        "estado": "REPORTADO",
        "prioridad": "BAJA",
        "reportadoEn": "2026-11-05T01:00:00Z",
        "analistaId": None,
        "tiempoRestanteMin": 2040,
        "relojPausado": False,
    }
    (filtro,) = repo.filtros
    assert (filtro.bandeja.value, filtro.texto, filtro.limite, filtro.prioridad.value) == (  # type: ignore[union-attr]
        "SIN_ASIGNAR",
        "0442",
        5,
        "BAJA",
    )
    assert filtro.actor_id == UUID(ANALISTA)
    assert filtro.corte == ahora


@pytest.mark.parametrize(
    "consulta",
    [
        {"limite": 0},
        {"limite": 101},
        {"q": ""},
        {"estado": "ABIERTO"},
        {"bandeja": "TODAS"},
        {"cursor": "x" * 201},
    ],
)
def test_parametros_invalidos_de_la_bandeja_son_422(cliente: TestClient, consulta: dict) -> None:  # type: ignore[type-arg]
    respuesta = cliente.get("/v1/siniestros", params=consulta, headers=CABECERAS)
    assert respuesta.status_code == 422
    assert error_b2(respuesta)["codigo"] == "SOLICITUD_INVALIDA"


def test_un_cursor_malo_es_422_con_su_campo(cliente: TestClient) -> None:
    respuesta = cliente.get(
        "/v1/siniestros", params={"cursor": "cursor-invalido"}, headers=CABECERAS
    )
    assert respuesta.status_code == 422
    cuerpo = error_b2(respuesta)
    assert (cuerpo["codigo"], cuerpo["detalles"]) == ("SOLICITUD_INVALIDA", {"campos": ["cursor"]})


def test_resumen_con_los_indicadores_del_dia(cliente: TestClient, repo) -> None:  # type: ignore[no-untyped-def]
    repo.indicadores_ = Indicadores(5, 1, 1, 3, 2, 21.5, 48, 5, 3)
    respuesta = cliente.get(
        "/v1/siniestros/resumen",
        params={"desde": "2026-11-05T05:00:00Z", "hasta": "2026-11-06T05:00:00Z"},
        headers=CABECERAS,
    )
    assert respuesta.status_code == 200
    assert respuesta.json() == {
        "pendientes": 5,
        "vencenHoy": 1,
        "fueraDeCompromiso": 1,
        "resueltosSemana": 3,
        "resueltosSemanaPrevia": 2,
        "tiempoMedioDecisionHoras": 21.5,
        "compromisoHoras": 48,
        "asignadosAlActor": 5,
        "sinAsignar": 3,
    }


@pytest.mark.parametrize(
    "consulta",
    [
        {"desde": "2026-11-06T05:00:00Z", "hasta": "2026-11-05T05:00:00Z"},
        {"desde": "2026-11-05T05:00:00", "hasta": "2026-11-06T05:00:00"},
        {"desde": "2026-11-05T05:00:00Z"},
    ],
)
def test_rango_invalido_del_resumen_es_422(cliente: TestClient, consulta: dict) -> None:  # type: ignore[type-arg]
    respuesta = cliente.get("/v1/siniestros/resumen", params=consulta, headers=CABECERAS)
    assert (respuesta.status_code, error_b2(respuesta)["codigo"]) == (422, "SOLICITUD_INVALIDA")


# --- Tomar el siguiente ----------------------------------------------------------------------------
def test_siguiente_asigna_y_la_misma_clave_devuelve_lo_mismo(
    cliente: TestClient,
    repo,
    siniestro: Callable[..., Siniestro],  # type: ignore[no-untyped-def]
) -> None:
    caso = siniestro()
    repo.agregar(caso)
    clave = {"Idempotency-Key": str(uuid4())}
    uno = cliente.post("/v1/siniestros/siguiente", headers={**CABECERAS, **clave})
    otra_vez = cliente.post("/v1/siniestros/siguiente", headers={**CABECERAS, **clave})
    assert uno.status_code == otra_vez.status_code == 200
    assert uno.json()["estado"] == "EN_EVALUACION"
    assert uno.json()["analistaId"] == ANALISTA
    assert uno.json()["id"] == otra_vez.json()["id"] == str(caso.id)
    assert len(repo.eventos) == 1


def test_siguiente_con_la_cola_vacia_es_404(cliente: TestClient) -> None:
    respuesta = cliente.post(
        "/v1/siniestros/siguiente", headers={**CABECERAS, "Idempotency-Key": str(uuid4())}
    )
    assert respuesta.status_code == 404
    assert error_b2(respuesta)["codigo"] == "COLA_SIN_CASOS"


@pytest.mark.parametrize("clave", [None, "no-es-uuid"])
def test_siguiente_exige_la_clave_de_idempotencia(cliente: TestClient, clave: str | None) -> None:
    cabeceras = dict(CABECERAS)
    if clave:
        cabeceras["Idempotency-Key"] = clave
    respuesta = cliente.post("/v1/siniestros/siguiente", headers=cabeceras)
    assert (respuesta.status_code, error_b2(respuesta)["codigo"]) == (422, "SOLICITUD_INVALIDA")


def test_la_misma_clave_de_otro_analista_es_422_conflicto(
    cliente: TestClient,
    repo,
    siniestro: Callable[..., Siniestro],  # type: ignore[no-untyped-def]
) -> None:
    repo.agregar(siniestro())
    clave = {"Idempotency-Key": str(uuid4())}
    cliente.post("/v1/siniestros/siguiente", headers={**CABECERAS, **clave})
    otro = {**CABECERAS, "X-Actor-Id": str(uuid4()), **clave}
    respuesta = cliente.post("/v1/siniestros/siguiente", headers=otro)
    assert (respuesta.status_code, error_b2(respuesta)["codigo"]) == (422, "IDEMPOTENCIA_CONFLICTO")


# --- Accesos denegados -----------------------------------------------------------------------------
def test_accesos_denegados_registra_con_origen_bff_y_responde_202_sin_cuerpo(
    cliente: TestClient,
    repo,  # type: ignore[no-untyped-def]
) -> None:
    respuesta = cliente.post(
        "/v1/siniestros/accesos-denegados",
        json={
            "actorId": "sub-cliente",
            "recurso": "SINIESTRO",
            "recursoId": str(uuid4()),
            "motivo": "SIN_GRUPO",
        },
        headers={"X-Actor-Id": "sub-cliente", "X-Actor-Rol": "CLIENTE"},
    )
    assert respuesta.status_code == 202
    assert respuesta.content == b""
    acceso = repo.eventos[0][1]
    assert (acceso.actor_id, acceso.rol, acceso.motivo.value, acceso.origen.value) == (
        "sub-cliente",
        "CLIENTE",
        "SIN_GRUPO",
        "BFF_WEB",
    )


def test_accesos_denegados_no_valida_el_rol(cliente: TestClient, repo) -> None:  # type: ignore[no-untyped-def]
    respuesta = cliente.post(
        "/v1/siniestros/accesos-denegados",
        json={"actorId": "a", "recurso": "BANDEJA", "recursoId": None, "motivo": "SIN_GRUPO"},
        headers={"X-Actor-Id": "bff"},
    )
    assert respuesta.status_code == 202
    assert repo.eventos[0][1].rol is None


@pytest.mark.parametrize(
    "cuerpo",
    [
        {"actorId": "", "recurso": "BANDEJA", "recursoId": None, "motivo": "SIN_GRUPO"},
        {"actorId": "a", "recurso": "NOMINA", "recursoId": None, "motivo": "SIN_GRUPO"},
        {"actorId": "a", "recurso": "BANDEJA", "recursoId": None, "motivo": "OTRO"},
        {"actorId": "a", "recurso": "BANDEJA", "recursoId": "x" * 65, "motivo": "SIN_GRUPO"},
    ],
)
def test_accesos_denegados_con_cuerpo_invalido_es_422(
    cliente: TestClient,
    repo,
    cuerpo: dict,  # type: ignore[no-untyped-def,type-arg]
) -> None:
    respuesta = cliente.post(
        "/v1/siniestros/accesos-denegados", json=cuerpo, headers={"X-Actor-Id": "bff"}
    )
    assert (respuesta.status_code, error_b2(respuesta)["codigo"]) == (422, "SOLICITUD_INVALIDA")
    assert repo.eventos == []


def test_accesos_denegados_sin_actor_es_401(cliente: TestClient) -> None:
    respuesta = cliente.post(
        "/v1/siniestros/accesos-denegados",
        json={"actorId": "a", "recurso": "BANDEJA", "recursoId": None, "motivo": "SIN_GRUPO"},
    )
    assert respuesta.status_code == 401


# --- Detalle ---------------------------------------------------------------------------------------
@pytest.fixture
def caso_con_archivos(repo, siniestro, evidencia, entrada, ahora):  # type: ignore[no-untyped-def]
    caso = siniestro(
        estado=EstadoSiniestro.EN_EVALUACION,
        analista_id=UUID(ANALISTA),
        asignado_en=ahora - timedelta(hours=12),
    )
    foto = evidencia(siniestro_id=caso.id, tipo_contenido="image/jpeg")
    repo.agregar(caso, foto)
    repo.historial.append(
        entrada(
            "ASIGNADO",
            ahora - timedelta(hours=12),
            siniestro_id=caso.id,
            estado_anterior=EstadoSiniestro.REPORTADO,
            estado_nuevo=EstadoSiniestro.EN_EVALUACION,
            actor_id=ANALISTA,
            actor_rol="ANALISTA_SINIESTROS",
            actor_nombre="Luis Mora",
        )
    )
    return caso, foto


def test_detalle_200_con_las_secciones_del_contrato(
    cliente: TestClient, caso_con_archivos, almacen
) -> None:  # type: ignore[no-untyped-def]
    caso, foto = caso_con_archivos
    respuesta = cliente.get(f"/v1/siniestros/{caso.id}", headers=CABECERAS)
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == "EN_EVALUACION"
    assert cuerpo["causa"] == "DANO_ACCIDENTAL"
    assert cuerpo["poliza"]["numero"] == "220-2026-011294"
    assert cuerpo["poliza"]["vigencia"] == {"desde": "2026-05-09", "hasta": "2027-05-09"}
    assert cuerpo["poliza"]["beneficiario"] == {
        "beneficiarioRef": "ben-sint-004",
        "cuentaMascarada": "****1004",
    }
    assert [v["codigo"] for v in cuerpo["verificacionCobertura"]] == [
        "VIGENCIA",
        "AMPARO",
        "CARENCIA",
        "PLAZO_AVISO",
        "PRIMAS",
        "SOPORTES",
    ]
    assert cuerpo["coherencia"]["resultado"] == "COHERENTE"
    assert cuerpo["evidencias"] == [
        {
            "id": str(foto.id),
            "nombreArchivo": "evidencia_dano_01.jpg",
            "tipo": "FOTOGRAFIA",
            "capturadaEn": "2026-11-05T01:07:00Z",
            "ubicacion": {"descripcion": None, "lat": 4.6512, "lon": -74.056},
            "integridad": "VERIFICADA",
        }
    ]
    assert [p["tipo"] for p in cuerpo["soportesPendientes"]] == [
        "FACTURA_COMPRA",
        "DOCUMENTO_IDENTIDAD",
        "CERTIFICADO_REPARACION",
    ]
    assert cuerpo["tramite"][2] == {
        "paso": "EN_EVALUACION",
        "estado": "ACTUAL",
        "ocurridoEn": "2026-11-05T03:00:00Z",
        "actorNombre": "Luis Mora",
    }
    assert cuerpo["historial"][0]["actorNombre"] == "Luis Mora"
    assert (cuerpo["liquidacion"], cuerpo["evento"], cuerpo["pago"]) == (None, None, None)


def test_el_detalle_nunca_trae_la_clave_del_objeto_ni_una_url(
    cliente: TestClient, caso_con_archivos, almacen
) -> None:  # type: ignore[no-untyped-def]
    caso, foto = caso_con_archivos
    texto = cliente.get(f"/v1/siniestros/{caso.id}", headers=CABECERAS).text
    assert foto.clave_objeto not in texto
    assert foto.sha256 not in texto
    assert "http" not in texto
    assert "claveObjeto" not in texto
    assert almacen.firmadas == []


def test_detalle_con_un_objeto_ausente_sigue_siendo_200(
    cliente: TestClient, caso_con_archivos, almacen
) -> None:  # type: ignore[no-untyped-def]
    caso, foto = caso_con_archivos
    almacen.integridad[foto.clave_objeto] = IntegridadEvidencia.NO_DISPONIBLE
    respuesta = cliente.get(f"/v1/siniestros/{caso.id}", headers=CABECERAS)
    assert respuesta.status_code == 200
    assert respuesta.json()["evidencias"][0]["integridad"] == "NO_DISPONIBLE"


def test_detalle_inexistente_o_con_id_malo(cliente: TestClient) -> None:
    respuesta = cliente.get(f"/v1/siniestros/{uuid4()}", headers=CABECERAS)
    assert (respuesta.status_code, error_b2(respuesta)["codigo"]) == (
        404,
        "SINIESTRO_NO_ENCONTRADO",
    )
    malo = cliente.get("/v1/siniestros/no-es-uuid", headers=CABECERAS)
    assert (malo.status_code, error_b2(malo)["codigo"]) == (422, "SOLICITUD_INVALIDA")


# --- Enlace ----------------------------------------------------------------------------------------
def test_enlace_200_y_el_log_de_cierre_no_lleva_la_url(
    cliente: TestClient,
    caso_con_archivos,
    almacen,
    caplog: pytest.LogCaptureFixture,
    repo,  # type: ignore[no-untyped-def]
) -> None:
    caso, foto = caso_con_archivos
    with caplog.at_level(logging.INFO):
        respuesta = cliente.post(
            f"/v1/siniestros/{caso.id}/evidencias/{foto.id}/enlace", headers=CABECERAS
        )
    assert respuesta.status_code == 200
    assert respuesta.json() == {
        "url": almacen.enlace,
        "expiraEn": "2026-11-05T15:05:00Z",
        "tipoContenido": "image/jpeg",
    }
    registros = "\n".join(
        f"{r.getMessage()} {json.dumps({k: str(v) for k, v in r.__dict__.items()})}"
        for r in caplog.records
    )
    assert "X-Amz-Signature" not in registros
    assert almacen.enlace not in registros
    assert foto.clave_objeto not in registros
    cierre = next(r for r in caplog.records if r.name == "siniestros.http")
    assert (cierre.siniestro_id, cierre.operacion, cierre.resultado) == (  # type: ignore[attr-defined]
        str(caso.id),
        "evidencia.enlace",
        "ok",
    )
    assert "Luis Mora" not in registros


def test_enlace_de_un_objeto_ausente_es_404_sin_url(
    cliente: TestClient, caso_con_archivos, almacen
) -> None:  # type: ignore[no-untyped-def]
    caso, foto = caso_con_archivos
    almacen.integridad[foto.clave_objeto] = IntegridadEvidencia.NO_DISPONIBLE
    respuesta = cliente.post(
        f"/v1/siniestros/{caso.id}/evidencias/{foto.id}/enlace", headers=CABECERAS
    )
    assert (respuesta.status_code, error_b2(respuesta)["codigo"]) == (
        404,
        "EVIDENCIA_NO_DISPONIBLE",
    )
    assert almacen.enlace not in respuesta.text


def test_enlace_con_el_almacen_caido_es_503(
    cliente: TestClient, caso_con_archivos, almacen
) -> None:  # type: ignore[no-untyped-def]
    caso, foto = caso_con_archivos
    almacen.falla_al_firmar = True
    respuesta = cliente.post(
        f"/v1/siniestros/{caso.id}/evidencias/{foto.id}/enlace", headers=CABECERAS
    )
    assert respuesta.status_code == 503
    cuerpo = error_b2(respuesta)
    assert (cuerpo["codigo"], cuerpo["detalles"]) == (
        "DEPENDENCIA_NO_DISPONIBLE",
        {"dependencia": "evidencias"},
    )


def test_enlace_de_recursos_inexistentes_es_404(cliente: TestClient, caso_con_archivos) -> None:  # type: ignore[no-untyped-def]
    caso, _ = caso_con_archivos
    sin_siniestro = cliente.post(
        f"/v1/siniestros/{uuid4()}/evidencias/{uuid4()}/enlace", headers=CABECERAS
    )
    assert error_b2(sin_siniestro)["codigo"] == "SINIESTRO_NO_ENCONTRADO"
    sin_evidencia = cliente.post(
        f"/v1/siniestros/{caso.id}/evidencias/{uuid4()}/enlace", headers=CABECERAS
    )
    assert error_b2(sin_evidencia)["codigo"] == "EVIDENCIA_NO_ENCONTRADA"


# --- Otros -----------------------------------------------------------------------------------------
def test_sin_base_la_app_atiende_el_health_pero_no_las_rutas() -> None:
    cliente = TestClient(crear_app())
    assert cliente.get("/health").json() == {"status": "ok"}
    respuesta = cliente.get("/v1/siniestros", headers=CABECERAS)
    assert (respuesta.status_code, error_b2(respuesta)["codigo"]) == (
        503,
        "DEPENDENCIA_NO_DISPONIBLE",
    )


def test_cabecera_de_correlacion_en_la_respuesta(cliente: TestClient) -> None:
    respuesta = cliente.get(
        "/v1/siniestros", headers={**CABECERAS, "X-Correlation-Id": "corr-w19-http"}
    )
    assert respuesta.headers["X-Correlation-Id"] == "corr-w19-http"


# --- Nombre del analista ---------------------------------------------------------------------------
@pytest.mark.parametrize("nombre", ["Luis Mora", "José Muñoz Peña", "李雷"])
def test_el_nombre_del_analista_con_tildes_llega_completo_al_historial(
    cliente: TestClient,
    repo,
    siniestro: Callable[..., Siniestro],
    nombre: str,  # type: ignore[no-untyped-def]
) -> None:
    repo.agregar(siniestro())
    respuesta = cliente.post(
        "/v1/siniestros/siguiente",
        headers={
            **CABECERAS,
            "X-Actor-Nombre": nombre.encode("utf-8"),
            "Idempotency-Key": str(uuid4()),
        },  # type: ignore[dict-item]
    )
    assert respuesta.status_code == 200
    assert repo.historial[0].actor_nombre == nombre


@pytest.mark.parametrize(
    ("recibido", "esperado"),
    [
        (None, None),
        ("Luis Mora", "Luis Mora"),
        ("José".encode().decode("latin-1"), "José"),
        ("Jos\xe9", "Jos\xe9"),  # latin-1 real, no UTF-8: se deja tal cual
        ("李雷", "李雷"),  # ya es texto: no cabe en latin-1
    ],
)
def test_recuperar_el_nombre_en_utf8(recibido: str | None, esperado: str | None) -> None:
    from siniestros.adapters.inbound.http.siniestros import _nombre_utf8

    assert _nombre_utf8(recibido) == esperado
