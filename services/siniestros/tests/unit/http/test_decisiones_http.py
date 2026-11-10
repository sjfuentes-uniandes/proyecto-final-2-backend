"""Rutas de la decisión del analista con casos de uso reales sobre un repositorio en memoria
(HU-W20, SEC-W20-01)."""

import json
import logging
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from siniestros.adapters.inbound.http.siniestros import Servicios
from siniestros.application.siniestro.acceso import RegistrarAccesoDenegado
from siniestros.application.siniestro.bandeja import ConsultarResumen, ListarBandeja
from siniestros.application.siniestro.decidir import Decidir
from siniestros.application.siniestro.detalle import ConsultarDetalle
from siniestros.application.siniestro.enlace import EmitirEnlace
from siniestros.application.siniestro.tomar import TomarSiguiente
from siniestros.application.siniestro.vistas import Plazos
from siniestros.domain.siniestro.modelos import EstadoSiniestro, Siniestro
from siniestros.main import crear_app

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")
ANALISTA = "a0a0a0a0-0000-4000-8000-000000000001"
OTRO = "a0a0a0a0-0000-4000-8000-000000000002"
CABECERAS = {
    "X-Actor-Id": ANALISTA,
    "X-Actor-Rol": "ANALISTA_SINIESTROS",
    "X-Actor-Nombre": "Luis Mora",
}
EXPLICACION = "EXPLICACION-PRIVADA-AL-CLIENTE"
MENSAJE = "MENSAJE-PRIVADO-AL-CLIENTE"


@pytest.fixture
def cliente(repo, almacen, soportes, reloj, plazos: Plazos) -> Iterator[TestClient]:  # type: ignore[no-untyped-def]
    app = crear_app()
    app.state.servicios = Servicios(
        bandeja=ListarBandeja(repo, reloj, plazos),
        resumen=ConsultarResumen(repo, reloj, plazos),
        tomar=TomarSiguiente(repo, reloj, plazos),
        detalle=ConsultarDetalle(repo, almacen, soportes, reloj, plazos),
        enlace=EmitirEnlace(repo, almacen, reloj, 300),
        decidir=Decidir(repo, reloj),
        acceso=RegistrarAccesoDenegado(repo, reloj),
        reloj=reloj,
        plazos=plazos,
    )
    yield TestClient(app)


@pytest.fixture
def caso(repo, siniestro: Callable[..., Siniestro], ahora: datetime) -> Siniestro:  # type: ignore[no-untyped-def]
    nuevo = siniestro(
        estado=EstadoSiniestro.EN_EVALUACION,
        analista_id=UUID(ANALISTA),
        asignado_en=ahora - timedelta(hours=13),
    )
    repo.agregar(nuevo)
    return nuevo


def aprobar(**cambios: Any) -> dict[str, Any]:
    cuerpo: dict[str, Any] = {
        "tipo": "APROBAR",
        "justificacion": "Daño verificado.",
        "liquidacion": {
            "valorReclamado": "2850000",
            "deducible": "350000",
            "valorIndemnizar": "2500000",
            "moneda": "COP",
        },
    }
    cuerpo.update(cambios)
    return cuerpo


def rechazar(**cambios: Any) -> dict[str, Any]:
    cuerpo: dict[str, Any] = {
        "tipo": "RECHAZAR",
        "justificacion": "La póliza había vencido.",
        "causal": "POLIZA_NO_VIGENTE",
        "explicacionCliente": EXPLICACION,
    }
    cuerpo.update(cambios)
    return cuerpo


def soporte(**cambios: Any) -> dict[str, Any]:
    cuerpo: dict[str, Any] = {
        "tipo": "SOLICITAR_SOPORTE",
        "justificacion": "Falta el certificado.",
        "documentos": ["CERTIFICADO_REPARACION"],
        "mensajeCliente": MENSAJE,
        "canal": "PUSH_Y_CORREO",
        "plazoDiasHabiles": 10,
    }
    cuerpo.update(cambios)
    return cuerpo


def decidir(cliente: TestClient, caso: Siniestro, cuerpo: Any, **cabeceras: str) -> Any:
    return cliente.post(
        f"/v1/siniestros/{caso.id}/decision",
        json=cuerpo,
        headers={**CABECERAS, "Idempotency-Key": str(uuid4()), **cabeceras},
    )


def error_b2(respuesta: Any) -> dict[str, Any]:
    cuerpo = respuesta.json()
    assert set(cuerpo) == {"codigo", "mensaje", "correlationId", "detalles"}
    return cuerpo  # type: ignore[no-any-return]


# --- El catálogo ------------------------------------------------------------------------------------


def test_el_catalogo_trae_las_listas_cerradas_como_codigos(cliente: TestClient) -> None:
    respuesta = cliente.get("/v1/siniestros/catalogos-decision", headers=CABECERAS)
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["causales"][0] == "POLIZA_NO_VIGENTE" and len(cuerpo["causales"]) == 7
    assert cuerpo["documentos"] == [
        "CERTIFICADO_REPARACION",
        "COTIZACION_REPUESTOS",
        "DENUNCIA_AUTORIDAD",
        "PERITAJE",
    ]
    assert cuerpo["canales"] == ["PUSH_Y_CORREO", "PUSH", "CORREO"]
    assert cuerpo["plazosDiasHabiles"] == [5, 10, 15]


def test_el_catalogo_no_se_confunde_con_un_siniestro(cliente: TestClient) -> None:
    # `/{siniestroId}` exigiría un UUID: si el catálogo no estuviera antes, sería 422.
    assert cliente.get("/v1/siniestros/catalogos-decision", headers=CABECERAS).status_code == 200


def test_el_catalogo_exige_actor_y_rol(cliente: TestClient, repo) -> None:  # type: ignore[no-untyped-def]
    sin_actor = cliente.get("/v1/siniestros/catalogos-decision")
    assert (sin_actor.status_code, error_b2(sin_actor)["codigo"]) == (401, "ACTOR_REQUERIDO")
    otro_rol = cliente.get(
        "/v1/siniestros/catalogos-decision", headers={**CABECERAS, "X-Actor-Rol": "CLIENTE"}
    )
    assert (otro_rol.status_code, error_b2(otro_rol)["codigo"]) == (403, "ROL_NO_AUTORIZADO")
    assert repo.eventos[0][1].recurso.value == "SINIESTRO"


# --- Las tres decisiones ----------------------------------------------------------------------------


def test_aprobar_responde_200_en_camel_case(cliente: TestClient, caso: Siniestro) -> None:
    respuesta = decidir(cliente, caso, aprobar())
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert set(cuerpo) == {
        "siniestroId",
        "estadoNuevo",
        "decisionId",
        "referenciaPago",
        "plazoSoporteVenceEn",
        "registradaEn",
    }
    assert cuerpo["estadoNuevo"] == "APROBADO"
    assert cuerpo["referenciaPago"] == "IND-88-2026-0442-01"
    assert cuerpo["plazoSoporteVenceEn"] is None
    assert cuerpo["registradaEn"] == "2026-11-05T15:00:00Z"


def test_rechazar_y_solicitar_soporte(
    cliente: TestClient,
    caso: Siniestro,
    repo,
    siniestro,
    ahora,  # type: ignore[no-untyped-def]
) -> None:
    rechazado = decidir(cliente, caso, rechazar())
    assert (rechazado.status_code, rechazado.json()["estadoNuevo"]) == (200, "RECHAZADO")
    assert rechazado.json()["referenciaPago"] is None

    otro = siniestro(
        codigo="88-2026-0443",
        estado=EstadoSiniestro.EN_EVALUACION,
        analista_id=UUID(ANALISTA),
        asignado_en=ahora,
    )
    repo.agregar(otro)
    pedido = decidir(cliente, otro, soporte())
    assert (pedido.status_code, pedido.json()["estadoNuevo"]) == (200, "FALTA_SOPORTE")
    assert pedido.json()["plazoSoporteVenceEn"] == "2026-11-19T15:00:00Z"


def test_repetir_con_la_misma_clave_devuelve_el_mismo_resultado(
    cliente: TestClient,
    caso: Siniestro,
    repo,  # type: ignore[no-untyped-def]
) -> None:
    clave = str(uuid4())
    primero = decidir(cliente, caso, aprobar(), **{"Idempotency-Key": clave})
    segundo = decidir(cliente, caso, aprobar(), **{"Idempotency-Key": clave})
    assert segundo.status_code == 200 and segundo.json() == primero.json()
    assert len(repo.decisiones) == 1 and len(repo.eventos) == 1


def test_la_misma_clave_con_otro_cuerpo_es_422_conflicto(
    cliente: TestClient, caso: Siniestro
) -> None:
    clave = str(uuid4())
    decidir(cliente, caso, aprobar(), **{"Idempotency-Key": clave})
    respuesta = decidir(cliente, caso, rechazar(), **{"Idempotency-Key": clave})
    assert (respuesta.status_code, error_b2(respuesta)["codigo"]) == (
        422,
        "IDEMPOTENCIA_CONFLICTO",
    )


@pytest.mark.parametrize("clave", [None, "no-es-uuid"])
def test_la_decision_exige_una_clave_de_idempotencia_uuid(
    cliente: TestClient, caso: Siniestro, clave: str | None
) -> None:
    cabeceras = dict(CABECERAS)
    if clave:
        cabeceras["Idempotency-Key"] = clave
    respuesta = cliente.post(
        f"/v1/siniestros/{caso.id}/decision", json=aprobar(), headers=cabeceras
    )
    assert (respuesta.status_code, error_b2(respuesta)["codigo"]) == (422, "SOLICITUD_INVALIDA")
    assert error_b2(respuesta)["detalles"]["campos"] == ["Idempotency-Key"]


# --- Errores estables -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cuerpo", "codigo", "detalles"),
    [
        (aprobar(justificacion=" "), "JUSTIFICACION_REQUERIDA", {}),
        (rechazar(causal="OTRA"), "CAUSAL_INVALIDA", {}),
        (
            aprobar(
                liquidacion={
                    "valorReclamado": "2850000",
                    "deducible": "350000",
                    "valorIndemnizar": "9999999",
                    "moneda": "COP",
                }
            ),
            "VALOR_FUERA_DE_RANGO",
            {"campos": ["liquidacion.valorIndemnizar"]},
        ),
        (
            rechazar(explicacionCliente=""),
            "SOLICITUD_INVALIDA",
            {"campos": ["explicacionCliente"]},
        ),
    ],
)
def test_los_errores_de_contenido_son_422_con_su_codigo(
    cliente: TestClient, caso: Siniestro, cuerpo: dict[str, Any], codigo: str, detalles: dict
) -> None:
    respuesta = decidir(cliente, caso, cuerpo)
    assert respuesta.status_code == 422
    resultado = error_b2(respuesta)
    assert (resultado["codigo"], resultado["detalles"]) == (codigo, detalles)


@pytest.mark.parametrize(
    ("cuerpo", "campos"),
    [
        (soporte(documentos=[]), ["documentos"]),
        (soporte(documentos=["FACTURA_COMPRA"]), ["documentos.0"]),
        (soporte(canal="SMS"), ["canal"]),
        (soporte(plazoDiasHabiles=7), ["plazoDiasHabiles"]),
        (rechazar(liquidacion={}), ["liquidacion"]),
        (aprobar(causal="POLIZA_NO_VIGENTE"), ["causal"]),
        ({"justificacion": "x"}, ["tipo"]),
        ({"tipo": "ANULAR", "justificacion": "x"}, ["tipo"]),
        ([1, 2], ["tipo"]),
        (
            aprobar(liquidacion={"valorReclamado": "1"}),
            ["liquidacion.deducible", "liquidacion.valorIndemnizar", "liquidacion.moneda"],
        ),
    ],
)
def test_los_campos_invalidos_salen_sin_la_etiqueta_del_tipo(
    cliente: TestClient, caso: Siniestro, cuerpo: Any, campos: list[str]
) -> None:
    respuesta = decidir(cliente, caso, cuerpo)
    assert respuesta.status_code == 422
    resultado = error_b2(respuesta)
    assert resultado["codigo"] == "SOLICITUD_INVALIDA"
    assert resultado["detalles"]["campos"] == campos


def test_un_caso_ya_decidido_es_409_con_el_estado_actual(
    cliente: TestClient,
    caso: Siniestro,
    repo,  # type: ignore[no-untyped-def]
) -> None:
    decidir(cliente, caso, aprobar())
    antes = (len(repo.historial), len(repo.eventos), repo.siniestros[caso.id])
    respuesta = decidir(cliente, caso, rechazar())
    resultado = error_b2(respuesta)
    assert (respuesta.status_code, resultado["codigo"]) == (409, "TRANSICION_NO_PERMITIDA")
    assert resultado["detalles"] == {"estadoActual": "APROBADO"}
    assert (len(repo.historial), len(repo.eventos), repo.siniestros[caso.id]) == antes


def test_otro_analista_recibe_409_caso_asignado_a_otro(
    cliente: TestClient, caso: Siniestro
) -> None:
    respuesta = decidir(cliente, caso, aprobar(), **{"X-Actor-Id": OTRO})
    assert (respuesta.status_code, error_b2(respuesta)["codigo"]) == (409, "CASO_ASIGNADO_A_OTRO")


def test_un_siniestro_inexistente_es_404(cliente: TestClient) -> None:
    respuesta = cliente.post(
        f"/v1/siniestros/{uuid4()}/decision",
        json=aprobar(),
        headers={**CABECERAS, "Idempotency-Key": str(uuid4())},
    )
    assert (respuesta.status_code, error_b2(respuesta)["codigo"]) == (
        404,
        "SINIESTRO_NO_ENCONTRADO",
    )


def test_sin_actor_es_401_y_un_rol_ajeno_es_403_y_se_audita(
    cliente: TestClient,
    caso: Siniestro,
    repo,  # type: ignore[no-untyped-def]
) -> None:
    clave = {"Idempotency-Key": str(uuid4())}
    ruta = f"/v1/siniestros/{caso.id}/decision"
    assert cliente.post(ruta, json=aprobar(), headers=clave).status_code == 401
    ajeno = cliente.post(
        ruta, json=aprobar(), headers={**CABECERAS, **clave, "X-Actor-Rol": "CLIENTE"}
    )
    assert (ajeno.status_code, error_b2(ajeno)["codigo"]) == (403, "ROL_NO_AUTORIZADO")
    ((_, acceso),) = repo.eventos
    assert (acceso.recurso.value, acceso.recurso_id) == ("SINIESTRO", str(caso.id))
    assert repo.decisiones == []  # sin rol no se decide


# --- SEC-W20-01: lo privado no sale -----------------------------------------------------------------


def test_ni_los_registros_ni_las_respuestas_llevan_los_textos_al_cliente(
    cliente: TestClient,
    caso: Siniestro,
    repo,  # type: ignore[no-untyped-def]
    caplog: pytest.LogCaptureFixture,
    siniestro: Callable[..., Siniestro],
    ahora: datetime,
) -> None:
    otro = siniestro(
        codigo="88-2026-0443",
        estado=EstadoSiniestro.EN_EVALUACION,
        analista_id=UUID(ANALISTA),
        asignado_en=ahora,
    )
    repo.agregar(otro)
    with caplog.at_level(logging.DEBUG):
        respuestas = [decidir(cliente, caso, rechazar()), decidir(cliente, otro, soporte())]
    registros = "\n".join(
        f"{r.getMessage()} {json.dumps({k: str(v) for k, v in r.__dict__.items()})}"
        for r in caplog.records
    )
    for texto in (EXPLICACION, MENSAJE, "Daño", "La póliza había vencido", "Luis Mora"):
        assert texto not in registros
        assert all(texto not in r.text for r in respuestas)
    cierres = [r for r in caplog.records if r.getMessage() == "siniestro decidido"]
    assert [(c.operacion, c.resultado, c.tipo, c.estado_nuevo) for c in cierres] == [  # type: ignore[attr-defined]
        ("siniestro.decidir", "ok", "RECHAZAR", "RECHAZADO"),
        ("siniestro.decidir", "ok", "SOLICITAR_SOPORTE", "FALTA_SOPORTE"),
    ]
    # Y tampoco en el historial ni en los eventos: solo viven en la decisión.
    assert EXPLICACION not in repr([repo.historial, repo.eventos])
    assert MENSAJE not in repr([repo.historial, repo.eventos])


# --- El detalle trae lo que el portal necesita para decidir y para mostrar la decisión -------------


def test_el_detalle_trae_el_valor_reclamado_la_referencia_prevista_y_el_plazo(
    cliente: TestClient,
    caso: Siniestro,
    repo,
    ahora,  # type: ignore[no-untyped-def]
) -> None:
    from dataclasses import replace
    from decimal import Decimal

    repo.agregar(replace(caso, valor_reclamado=Decimal("2850000.00"), moneda="COP"))
    cuerpo = cliente.get(f"/v1/siniestros/{caso.id}", headers=CABECERAS).json()
    assert cuerpo["valorReclamado"] == "2850000.00"
    assert cuerpo["referenciaPagoPrevista"] == "IND-88-2026-0442-01"
    assert cuerpo["plazoSoporteVenceEn"] is None
    assert cuerpo["liquidacion"] is None


def test_el_detalle_de_un_caso_aprobado_trae_la_liquidacion(
    cliente: TestClient,
    repo,  # type: ignore[no-untyped-def]
    siniestro: Callable[..., Siniestro],
    ahora: datetime,
) -> None:
    from decimal import Decimal

    from siniestros.application.siniestro.vistas import LiquidacionAprobada

    aprobado = siniestro(
        estado=EstadoSiniestro.APROBADO,
        analista_id=UUID(ANALISTA),
        asignado_en=ahora,
        decidido_en=ahora,
        deducible=Decimal("350000.00"),
        valor_indemnizar=Decimal("2500000.00"),
    )
    repo.agregar(aprobado)
    repo.liquidaciones[aprobado.id] = LiquidacionAprobada(
        "COP",
        Decimal("2850000.00"),
        Decimal("350000.00"),
        Decimal("2500000.00"),
        "IND-88-2026-0442-01",
        ahora,
    )
    cuerpo = cliente.get(f"/v1/siniestros/{aprobado.id}", headers=CABECERAS).json()
    assert cuerpo["liquidacion"] == {
        "moneda": "COP",
        "valorReclamado": "2850000.00",
        "deducible": "350000.00",
        "valorIndemnizar": "2500000.00",
        "referenciaPago": "IND-88-2026-0442-01",
        "aprobadaEn": "2026-11-05T15:00:00Z",
    }


def test_el_detalle_de_un_caso_en_soporte_trae_el_plazo(
    cliente: TestClient,
    repo,  # type: ignore[no-untyped-def]
    siniestro: Callable[..., Siniestro],
    ahora: datetime,
) -> None:
    pedido = siniestro(
        estado=EstadoSiniestro.FALTA_SOPORTE,
        analista_id=UUID(ANALISTA),
        asignado_en=ahora,
        pausado_desde=ahora,
        plazo_soporte_vence_en=ahora + timedelta(days=14),
    )
    repo.agregar(pedido)
    cuerpo = cliente.get(f"/v1/siniestros/{pedido.id}", headers=CABECERAS).json()
    assert cuerpo["plazoSoporteVenceEn"] == "2026-11-19T15:00:00Z"
    assert cuerpo["relojPausado"] is True
