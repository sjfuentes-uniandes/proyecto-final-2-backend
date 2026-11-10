"""UT-W19 (bff-web): siniestros del analista (HU-W19) con siniestros, clientes y consulta simulados.

Solo `analistas-siniestros` entra (los demás, 403 y aviso a siniestros); actor, rol y nombre hacia
siniestros (DS3-10); el nombre del cliente y la póliza se componen con degradación a null; los 404 y
422 de siniestros pasan y todo lo demás es 503 SERVICIO_NO_DISPONIBLE; la URL del enlace no se
registra.
"""

import asyncio
import base64
import json
import logging
from collections.abc import Callable, Iterator
from typing import Any
from uuid import uuid4

import httpx
import pytest
from bff_web.adapters.inbound import siniestros as ruta
from bff_web.adapters.outbound.clientes import ClientesCliente
from bff_web.adapters.outbound.consulta import ConsultaCliente
from bff_web.adapters.outbound.siniestros import SiniestrosCliente
from bff_web.application.siniestros import (
    Siniestros,
    esperar_avisos,
    nombre_de_analista,
)
from bff_web.config import settings
from bff_web.main import app
from fastapi.testclient import TestClient

BACKOFFICE = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_back"
SUB = "a0a0a0a0-0000-4000-8000-000000000001"
SINIESTRO_ID = "5e000000-0000-4000-8000-000004000000"
EVIDENCIA_ID = "e0000000-0000-4000-8000-000004000001"
CLIENTE_ID = "00000000-0000-4000-8000-000000000001"
POLIZA_ID = "a0000000-0000-4000-8000-000004000000"
RESUMEN: dict[str, Any] = {
    "id": SINIESTRO_ID,
    "codigo": "88-2026-0442",
    "polizaId": POLIZA_ID,
    "polizaNumero": "220-2026-011294",
    "clienteId": CLIENTE_ID,
    "ramo": "Dispositivos",
    "origen": "MOVIL",
    "estado": "EN_EVALUACION",
    "prioridad": "BAJA",
    "reportadoEn": "2026-11-05T01:00:00Z",
    "analistaId": SUB,
    "tiempoRestanteMin": 2040,
    "relojPausado": False,
}
DETALLE: dict[str, Any] = {
    **RESUMEN,
    "causa": "DANO_ACCIDENTAL",
    "fechaEvento": "2026-11-05T00:19:00Z",
    "ubicacionDeclarada": {"descripcion": "Bogotá D.C.", "lat": 4.652, "lon": -74.0555},
    "poliza": {
        "numero": "220-2026-011294",
        "ramo": "Dispositivos",
        "vigencia": {"desde": "2026-05-09", "hasta": "2027-05-09"},
        "amparos": [
            {
                "codigo": "DANO_ACCIDENTAL",
                "nombre": "Daño accidental",
                "sumaAsegurada": "3000000.00",
            }
        ],
        "moneda": "COP",
        "carenciaDias": 30,
        "plazoAvisoDias": 30,
        "primasAlDia": True,
        "beneficiario": {"beneficiarioRef": "ben-sint-004", "cuentaMascarada": "****1004"},
    },
    "verificacionCobertura": [
        {"codigo": "VIGENCIA", "resultado": "CUMPLE", "detalle": {"fechaEvento": "2026-11-04"}}
    ],
    "coherencia": {
        "fechaDeclarada": "2026-11-05T00:19:00Z",
        "primeraCaptura": "2026-11-05T00:53:00Z",
        "diferenciaMin": 34,
        "ubicacionCoincide": True,
        "archivosAlterados": 0,
        "resultado": "COHERENTE",
    },
    "evidencias": [
        {
            "id": EVIDENCIA_ID,
            "nombreArchivo": "evidencia_dano_01.jpg",
            "tipo": "FOTOGRAFIA",
            "capturadaEn": "2026-11-05T00:53:00Z",
            "ubicacion": {"descripcion": None, "lat": 4.6512, "lon": -74.056},
            "integridad": "VERIFICADA",
        }
    ],
    "soportesPendientes": [{"tipo": "CERTIFICADO_REPARACION", "estado": "FALTA"}],
    "tramite": [
        {
            "paso": "RECIBIDO",
            "estado": "COMPLETADO",
            "ocurridoEn": "2026-11-05T01:00:00Z",
            "actorNombre": None,
        }
    ],
    "historial": [
        {
            "id": str(uuid4()),
            "accion": "ASIGNADO",
            "estadoAnterior": "REPORTADO",
            "estadoNuevo": "EN_EVALUACION",
            "actorId": SUB,
            "actorRol": "ANALISTA_SINIESTROS",
            "actorNombre": "Luis Mora",
            "justificacion": None,
            "ocurridoEn": "2026-11-05T03:00:00Z",
        }
    ],
    "liquidacion": None,
    "evento": None,
    "pago": None,
    "valorReclamado": None,
    "referenciaPagoPrevista": "IND-88-2026-0442-01",
    "plazoSoporteVenceEn": None,
}
BANDEJA: dict[str, Any] = {"items": [RESUMEN], "siguienteCursor": None, "total": 1}
INDICADORES: dict[str, Any] = {
    "pendientes": 5,
    "vencenHoy": 1,
    "fueraDeCompromiso": 1,
    "resueltosSemana": 0,
    "resueltosSemanaPrevia": 0,
    "tiempoMedioDecisionHoras": None,
    "compromisoHoras": 48,
    "asignadosAlActor": 5,
    "sinAsignar": 3,
}
POLIZA_CONSULTA: dict[str, Any] = {
    "polizaId": POLIZA_ID,
    "numero": "220-2026-011294",
    "estado": "VIGENTE",
    "productoId": "p",
    "productoVersion": "1",
    "productoNombre": "Dispositivos protegidos",
    "ramo": "Dispositivos",
    "vigencia": {"desde": "2026-05-09", "hasta": "2027-05-09"},
    "vigente": True,
    "prima": "10000.00",
    "sumaAsegurada": "3000000.00",
    "moneda": "COP",
    "periodicidad": "MENSUAL",
    "coberturas": [],
    "documentoHuella": "h" * 64,
    "firmaId": "f",
    "emitidaEn": "2026-05-09T10:00:00Z",
}
ENLACE = {
    "url": "https://s3.local/o?X-Amz-Signature=secreta",
    "expiraEn": "2026-11-05T15:05:00Z",
    "tipoContenido": "image/jpeg",
}
CATALOGOS = {
    "causales": [
        "POLIZA_NO_VIGENTE",
        "EVENTO_NO_CUBIERTO",
        "PERIODO_DE_CARENCIA",
        "AVISO_EXTEMPORANEO",
        "PRIMAS_EN_MORA",
        "EVIDENCIA_INCONSISTENTE",
        "EXCLUSION_CONTRACTUAL",
    ],
    "documentos": [
        "CERTIFICADO_REPARACION",
        "COTIZACION_REPUESTOS",
        "DENUNCIA_AUTORIDAD",
        "PERITAJE",
    ],
    "canales": ["PUSH_Y_CORREO", "PUSH", "CORREO"],
    "plazosDiasHabiles": [5, 10, 15],
}
RESULTADO_APROBADO = {
    "siniestroId": SINIESTRO_ID,
    "estadoNuevo": "APROBADO",
    "decisionId": "d0000000-0000-4000-8000-000000000001",
    "referenciaPago": "IND-88-2026-0442-01",
    "plazoSoporteVenceEn": None,
    "registradaEn": "2026-11-05T15:00:00Z",
}
EXPLICACION = "EXPLICACION-PRIVADA-AL-CLIENTE"
MENSAJE = "MENSAJE-PRIVADO-AL-CLIENTE"
APROBAR = {
    "tipo": "APROBAR",
    "justificacion": "Daño verificado.",
    "evidenciasConsideradas": [EVIDENCIA_ID],
    "liquidacion": {
        "valorReclamado": "2850000",
        "deducible": "350000",
        "valorIndemnizar": "2500000",
        "moneda": "COP",
    },
}
RECHAZAR = {
    "tipo": "RECHAZAR",
    "justificacion": "La póliza había vencido.",
    "causal": "POLIZA_NO_VIGENTE",
    "explicacionCliente": EXPLICACION,
}
SOPORTE = {
    "tipo": "SOLICITAR_SOPORTE",
    "justificacion": "Falta el certificado.",
    "documentos": ["CERTIFICADO_REPARACION"],
    "mensajeCliente": MENSAJE,
    "canal": "PUSH_Y_CORREO",
    "plazoDiasHabiles": 10,
}
NO_ENCONTRADO = {
    "codigo": "SINIESTRO_NO_ENCONTRADO",
    "mensaje": "El siniestro no existe",
    "correlationId": "c",
    "detalles": {},
}


def _token(carga: dict[str, Any]) -> str:
    def b64(datos: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(datos).encode()).decode().rstrip("=")

    return f"Bearer {b64({'alg': 'RS256'})}.{b64(carga)}.firma"


def _usuario(grupos: str, sub: str | None = SUB, **extra: str) -> dict[str, str]:
    encabezados = {
        "X-Authenticated-Groups": grupos,
        "X-Authenticated-Issuer": BACKOFFICE,
        "X-Correlation-Id": "corr-bff-w19",
        **extra,
    }
    if sub:
        encabezados["X-Authenticated-Sub"] = sub
    return encabezados


ANALISTA = _usuario("[analistas-siniestros]")


class Simulado:
    def __init__(self) -> None:
        self.pedidos: list[httpx.Request] = []
        self.pedidos_clientes: list[httpx.Request] = []
        self.pedidos_consulta: list[httpx.Request] = []
        self.responder: Callable[[httpx.Request], httpx.Response] = self._por_ruta
        self.responder_clientes: Callable[[httpx.Request], httpx.Response] = lambda _: (
            httpx.Response(200, json={"nombreCompleto": "Natalia González Ruiz"})
        )
        self.responder_consulta: Callable[[httpx.Request], httpx.Response] = lambda _: (
            httpx.Response(200, json=POLIZA_CONSULTA)
        )

    @staticmethod
    def _por_ruta(pedido: httpx.Request) -> httpx.Response:
        ruta_ = pedido.url.path
        if ruta_.endswith("/accesos-denegados"):
            return httpx.Response(202)
        if ruta_.endswith("/enlace"):
            return httpx.Response(200, json=ENLACE)
        if ruta_.endswith("/catalogos-decision"):
            return httpx.Response(200, json=CATALOGOS)
        if ruta_.endswith("/decision"):
            return httpx.Response(200, json=RESULTADO_APROBADO)
        if ruta_.endswith("/resumen"):
            return httpx.Response(200, json=INDICADORES)
        if ruta_.endswith("/siguiente"):
            return httpx.Response(200, json=RESUMEN)
        if ruta_ == "/v1/siniestros":
            return httpx.Response(200, json=BANDEJA)
        return httpx.Response(200, json=DETALLE)

    def siniestros(self, pedido: httpx.Request) -> httpx.Response:
        self.pedidos.append(pedido)
        return self.responder(pedido)

    def clientes(self, pedido: httpx.Request) -> httpx.Response:
        self.pedidos_clientes.append(pedido)
        return self.responder_clientes(pedido)

    def consulta(self, pedido: httpx.Request) -> httpx.Response:
        self.pedidos_consulta.append(pedido)
        return self.responder_consulta(pedido)

    @property
    def avisos(self) -> list[httpx.Request]:
        return [p for p in self.pedidos if p.url.path.endswith("/accesos-denegados")]


@pytest.fixture
def simulado(monkeypatch: pytest.MonkeyPatch) -> Iterator[Simulado]:
    sim = Simulado()
    monkeypatch.setattr(settings, "jwt_issuers", {"backoffice": BACKOFFICE})
    servicio = Siniestros(
        SiniestrosCliente("http://siniestros:8080", 1500, httpx.MockTransport(sim.siniestros)),
        ClientesCliente("http://clientes:8080", 5000, httpx.MockTransport(sim.clientes)),
        ConsultaCliente("http://consulta:8080", 700, httpx.MockTransport(sim.consulta)),
        nombre_timeout_ms=500,
        poliza_timeout_ms=700,
        aviso_timeout_ms=300,
    )
    app.dependency_overrides[ruta.siniestros_servicio] = lambda: servicio
    yield sim
    app.dependency_overrides.clear()


def _pedir(
    metodo: str,
    ruta_: str,
    encabezados: dict[str, str] = ANALISTA,
    *,
    headers: dict[str, str] | None = None,
    **opciones: Any,
) -> httpx.Response:
    return TestClient(app).request(metodo, ruta_, headers=headers or encabezados, **opciones)


def _json(estado: int, cuerpo: Any) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _: httpx.Response(estado, json=cuerpo)


def _lanza(error: type[httpx.HTTPError]) -> Callable[[httpx.Request], httpx.Response]:
    def responder(pedido: httpx.Request) -> httpx.Response:
        raise error("falla de siniestros", request=pedido)

    return responder


RUTAS = [
    ("GET", "/v1/siniestros"),
    ("GET", "/v1/siniestros/resumen?desde=2026-11-05T05:00:00Z&hasta=2026-11-06T05:00:00Z"),
    ("POST", "/v1/siniestros/siguiente"),
    ("GET", f"/v1/siniestros/{SINIESTRO_ID}"),
    ("POST", f"/v1/siniestros/{SINIESTRO_ID}/evidencias/{EVIDENCIA_ID}/enlace"),
]
IDEM = {"Idempotency-Key": "2b3f7a3e-77a0-4b8b-9d77-0e6f3b6e0a11"}


# --- Grupo ------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("metodo", "ruta_"), RUTAS)
@pytest.mark.parametrize("grupos", ["[asesores]", "[operacion]", "[suscriptores]", "[]", ""])
def test_sin_el_grupo_es_403_y_no_se_consulta_nada(
    simulado: Simulado, metodo: str, ruta_: str, grupos: str
) -> None:
    respuesta = (
        _pedir(metodo, ruta_, _usuario(grupos), headers=None)
        if False
        else TestClient(app).request(metodo, ruta_, headers={**_usuario(grupos), **IDEM})
    )
    assert respuesta.status_code == 403
    assert set(respuesta.json()) == {"codigo", "mensaje", "correlationId", "detalles"}
    assert respuesta.json()["codigo"] == "ROL_NO_AUTORIZADO"
    assert [p for p in simulado.pedidos if not p.url.path.endswith("/accesos-denegados")] == []
    assert simulado.pedidos_clientes == simulado.pedidos_consulta == []


def test_un_emisor_que_no_es_del_backoffice_es_403(simulado: Simulado) -> None:
    encabezados = {**ANALISTA, "X-Authenticated-Issuer": "https://otro-pool"}
    assert _pedir("GET", "/v1/siniestros", encabezados).status_code == 403
    assert [p for p in simulado.pedidos if not p.url.path.endswith("/accesos-denegados")] == []


def test_sin_usuario_autenticado_es_401(simulado: Simulado) -> None:
    respuesta = _pedir("GET", "/v1/siniestros", _usuario("[analistas-siniestros]", sub=None))
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (401, "ACTOR_REQUERIDO")


# --- Bandeja ----------------------------------------------------------------------------------------
def test_la_bandeja_agrega_el_nombre_del_cliente_y_reenvia_el_actor(simulado: Simulado) -> None:
    respuesta = _pedir(
        "GET",
        "/v1/siniestros",
        params={
            "bandeja": "SIN_ASIGNAR",
            "estado": "REPORTADO",
            "q": "0442",
            "limite": 5,
            "prioridad": "ALTA",
            "origen": "MOVIL",
            "cursor": "c1",
        },
    )
    assert respuesta.status_code == 200
    assert respuesta.json() == {
        **BANDEJA,
        "items": [{**RESUMEN, "clienteNombre": "Natalia González Ruiz"}],
    }
    [pedido] = simulado.pedidos
    assert pedido.url.path == "/v1/siniestros"
    assert dict(pedido.url.params) == {
        "bandeja": "SIN_ASIGNAR",
        "limite": "5",
        "estado": "REPORTADO",
        "origen": "MOVIL",
        "prioridad": "ALTA",
        "q": "0442",
        "cursor": "c1",
    }
    assert pedido.headers["X-Actor-Id"] == SUB
    assert pedido.headers["X-Actor-Rol"] == "ANALISTA_SINIESTROS"
    assert pedido.headers["X-Correlation-Id"] == "corr-bff-w19"
    assert pedido.headers["X-Actor-Nombre"] == SUB  # sin token: el sub


def test_por_omision_pide_mi_bandeja(simulado: Simulado) -> None:
    _pedir("GET", "/v1/siniestros")
    assert dict(simulado.pedidos[0].url.params) == {"bandeja": "MIA", "limite": "20"}


def test_pide_el_nombre_una_vez_por_cliente(simulado: Simulado) -> None:
    otro = {**RESUMEN, "id": str(uuid4()), "clienteId": "00000000-0000-4000-8000-000000000002"}
    simulado.responder = _json(
        200, {**BANDEJA, "items": [RESUMEN, {**RESUMEN, "id": str(uuid4())}, otro], "total": 3}
    )
    respuesta = _pedir("GET", "/v1/siniestros")
    assert [i["clienteNombre"] for i in respuesta.json()["items"]] == ["Natalia González Ruiz"] * 3
    assert len(simulado.pedidos_clientes) == 2


@pytest.mark.parametrize(
    "falla",
    [_json(500, {}), _lanza(httpx.ReadTimeout), _json(404, {"codigo": "X"}), _json(200, ["no"])],
)
def test_si_clientes_falla_el_nombre_es_null_y_la_bandeja_sale(
    simulado: Simulado, falla: Any
) -> None:
    simulado.responder_clientes = falla
    respuesta = _pedir("GET", "/v1/siniestros")
    assert respuesta.status_code == 200
    assert respuesta.json()["items"][0]["clienteNombre"] is None


def test_el_nombre_del_analista_sale_del_token(simulado: Simulado) -> None:
    token = _token({"sub": SUB, "name": "José Muñoz", "email": "jose@solventa.co"})
    _pedir("GET", "/v1/siniestros", {**ANALISTA, "Authorization": token})
    crudo = {k.lower(): v for k, v in simulado.pedidos[0].headers.raw}[b"x-actor-nombre"]
    assert crudo == "José Muñoz".encode()  # en UTF-8: siniestros lo recupera


# --- Resumen y siguiente ----------------------------------------------------------------------------
def test_el_resumen_reenvia_el_rango(simulado: Simulado) -> None:
    respuesta = _pedir(
        "GET",
        "/v1/siniestros/resumen",
        params={"desde": "2026-11-05T05:00:00Z", "hasta": "2026-11-06T05:00:00+00:00"},
    )
    assert (respuesta.status_code, respuesta.json()) == (200, INDICADORES)
    assert dict(simulado.pedidos[0].url.params) == {
        "desde": "2026-11-05T05:00:00+00:00",
        "hasta": "2026-11-06T05:00:00+00:00",
    }


@pytest.mark.parametrize(
    "consulta",
    [
        {"desde": "2026-11-05T05:00:00"},
        {"desde": "2026-11-05T05:00:00", "hasta": "2026-11-06T05:00:00"},
    ],
)
def test_un_rango_invalido_es_422_sin_llamar_a_siniestros(
    simulado: Simulado, consulta: dict[str, str]
) -> None:
    respuesta = _pedir("GET", "/v1/siniestros/resumen", params=consulta)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (422, "SOLICITUD_INVALIDA")
    assert simulado.pedidos == []


def test_siguiente_reenvia_la_clave_y_agrega_el_nombre(simulado: Simulado) -> None:
    respuesta = _pedir("POST", "/v1/siniestros/siguiente", headers={**ANALISTA, **IDEM})
    assert (respuesta.status_code, respuesta.json()["clienteNombre"]) == (
        200,
        "Natalia González Ruiz",
    )
    [pedido] = simulado.pedidos
    assert (pedido.method, pedido.url.path, pedido.headers["Idempotency-Key"]) == (
        "POST",
        "/v1/siniestros/siguiente",
        IDEM["Idempotency-Key"],
    )


def test_siguiente_sin_clave_valida_es_422(simulado: Simulado) -> None:
    assert _pedir("POST", "/v1/siniestros/siguiente").status_code == 422
    assert (
        _pedir(
            "POST", "/v1/siniestros/siguiente", headers={**ANALISTA, "Idempotency-Key": "x"}
        ).status_code
        == 422
    )
    assert simulado.pedidos == []


@pytest.mark.parametrize(
    ("estado", "codigo"),
    [(404, "COLA_SIN_CASOS"), (422, "IDEMPOTENCIA_CONFLICTO"), (422, "SOLICITUD_INVALIDA")],
)
def test_los_errores_del_contrato_de_siniestros_pasan(
    simulado: Simulado, estado: int, codigo: str
) -> None:
    simulado.responder = _json(
        estado,
        {"codigo": codigo, "mensaje": "m", "correlationId": "c", "detalles": {"campos": ["x"]}},
    )
    respuesta = _pedir("POST", "/v1/siniestros/siguiente", headers={**ANALISTA, **IDEM})
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (estado, codigo)
    assert respuesta.json()["detalles"] == {"campos": ["x"]}


# --- Detalle ----------------------------------------------------------------------------------------
def test_el_detalle_compone_el_cliente_y_la_poliza(simulado: Simulado) -> None:
    respuesta = _pedir("GET", f"/v1/siniestros/{SINIESTRO_ID}")
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["clienteNombre"] == "Natalia González Ruiz"
    assert cuerpo["polizaVista"] == {
        "polizaId": POLIZA_ID,
        "numero": "220-2026-011294",
        "estado": "VIGENTE",
        "ramo": "Dispositivos",
        "productoNombre": "Dispositivos protegidos",
        "vigencia": {"desde": "2026-05-09", "hasta": "2027-05-09"},
        "vigente": True,
        "sumaAsegurada": "3000000.00",
        "moneda": "COP",
    }
    assert {k: v for k, v in cuerpo.items() if k not in ("clienteNombre", "polizaVista")} == DETALLE
    [pedido] = simulado.pedidos_consulta
    assert pedido.url.path == f"/v1/clientes/{CLIENTE_ID}/polizas/{POLIZA_ID}"
    assert "documentoHuella" not in respuesta.text
    assert "firmaId" not in respuesta.text


@pytest.mark.parametrize(
    "falla",
    [
        _json(404, {"codigo": "POLIZA_NO_ENCONTRADA"}),
        _json(503, {}),
        _lanza(httpx.ReadTimeout),
        _json(200, {"numero": "x"}),
        _json(200, {**POLIZA_CONSULTA, "numero": "mal"}),
    ],
)
def test_si_consulta_falla_la_poliza_es_null_y_el_detalle_sale(
    simulado: Simulado, falla: Any
) -> None:
    simulado.responder_consulta = falla
    respuesta = _pedir("GET", f"/v1/siniestros/{SINIESTRO_ID}")
    assert respuesta.status_code == 200
    assert respuesta.json()["polizaVista"] is None
    assert respuesta.json()["clienteNombre"] == "Natalia González Ruiz"


def test_si_clientes_falla_el_nombre_es_null_y_el_detalle_sale(simulado: Simulado) -> None:
    simulado.responder_clientes = _json(503, {})
    cuerpo = _pedir("GET", f"/v1/siniestros/{SINIESTRO_ID}").json()
    assert cuerpo["clienteNombre"] is None
    assert cuerpo["polizaVista"] is not None


def test_un_siniestro_inexistente_pasa_como_404(simulado: Simulado) -> None:
    simulado.responder = _json(404, NO_ENCONTRADO)
    respuesta = _pedir("GET", f"/v1/siniestros/{SINIESTRO_ID}")
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (404, "SINIESTRO_NO_ENCONTRADO")
    assert simulado.pedidos_clientes == simulado.pedidos_consulta == []


def test_un_id_que_no_es_uuid_es_422_sin_llamar_a_siniestros(simulado: Simulado) -> None:
    assert _pedir("GET", "/v1/siniestros/no-es-uuid").status_code == 422
    assert simulado.pedidos == []


# --- Falla cerrada ----------------------------------------------------------------------------------
FALLAS = [
    _json(500, {"codigo": "ERROR_INTERNO"}),
    _json(503, {"codigo": "DEPENDENCIA_NO_DISPONIBLE"}),
    _json(401, {"codigo": "ACTOR_REQUERIDO"}),
    _json(403, {"codigo": "ROL_NO_AUTORIZADO"}),
    _json(404, {"codigo": "OTRO_CODIGO"}),
    _json(409, {"codigo": "SINIESTRO_NO_ENCONTRADO"}),
    _json(422, {"codigo": "TRANSICION_NO_PERMITIDA"}),
    _json(404, {"codigo": "CAUSAL_INVALIDA"}),
    _json(404, {}),
    lambda _: httpx.Response(200, text="no es json"),
    _lanza(httpx.ReadTimeout),
    _lanza(httpx.ConnectError),
]


@pytest.mark.parametrize(("metodo", "ruta_"), RUTAS)
@pytest.mark.parametrize("falla", FALLAS)
def test_todo_lo_demas_es_503_sin_detalles(
    simulado: Simulado, metodo: str, ruta_: str, falla: Any
) -> None:
    simulado.responder = falla
    respuesta = TestClient(app).request(metodo, ruta_, headers={**ANALISTA, **IDEM})
    assert respuesta.status_code == 503
    assert respuesta.json() == {
        "codigo": "SERVICIO_NO_DISPONIBLE",
        "mensaje": "El servicio de siniestros no está disponible",
        "correlationId": "corr-bff-w19",
        "detalles": {},
    }


@pytest.mark.parametrize(
    ("ruta_", "metodo", "cuerpo"),
    [
        ("/v1/siniestros", "GET", {"items": "no", "total": 1}),
        ("/v1/siniestros", "GET", {"items": [{"id": "x"}], "siguienteCursor": None, "total": 1}),
        (
            "/v1/siniestros/resumen?desde=2026-11-05T05:00:00Z&hasta=2026-11-06T05:00:00Z",
            "GET",
            {"pendientes": "x"},
        ),
        ("/v1/siniestros/siguiente", "POST", {"id": "x"}),
        (f"/v1/siniestros/{SINIESTRO_ID}", "GET", {**DETALLE, "estado": "ABIERTO"}),
        (f"/v1/siniestros/{SINIESTRO_ID}/evidencias/{EVIDENCIA_ID}/enlace", "POST", {"url": 1}),
    ],
)
def test_un_cuerpo_fuera_del_contrato_es_503(
    simulado: Simulado, ruta_: str, metodo: str, cuerpo: Any
) -> None:
    simulado.responder = _json(200, cuerpo)
    respuesta = TestClient(app).request(metodo, ruta_, headers={**ANALISTA, **IDEM})
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (503, "SERVICIO_NO_DISPONIBLE")


# --- Enlace -----------------------------------------------------------------------------------------
def test_el_enlace_pasa_tal_cual_y_no_se_registra(
    simulado: Simulado, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        respuesta = _pedir(
            "POST", f"/v1/siniestros/{SINIESTRO_ID}/evidencias/{EVIDENCIA_ID}/enlace"
        )
    assert (respuesta.status_code, respuesta.json()) == (200, ENLACE)
    assert (
        simulado.pedidos[0].url.path
        == f"/v1/siniestros/{SINIESTRO_ID}/evidencias/{EVIDENCIA_ID}/enlace"
    )
    registros = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "X-Amz-Signature" not in registros
    assert ENLACE["url"] not in registros


@pytest.mark.parametrize(
    "codigo", ["SINIESTRO_NO_ENCONTRADO", "EVIDENCIA_NO_ENCONTRADA", "EVIDENCIA_NO_DISPONIBLE"]
)
def test_los_tres_404_del_enlace_pasan(simulado: Simulado, codigo: str) -> None:
    simulado.responder = _json(
        404, {"codigo": codigo, "mensaje": "m", "correlationId": "c", "detalles": {}}
    )
    respuesta = _pedir("POST", f"/v1/siniestros/{SINIESTRO_ID}/evidencias/{EVIDENCIA_ID}/enlace")
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (404, codigo)


# --- Nombre del analista ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("carga", "esperado"),
    [
        ({"name": "Luis Mora", "email": "lmora@solventa.co"}, "Luis Mora"),
        ({"email": "lmora@solventa.co"}, "lmora"),
        ({"name": "  ", "email": "lmora@solventa.co"}, "lmora"),
        ({"name": 7, "email": 8}, SUB),
        ({"email": "sin-arroba"}, SUB),
        ({}, SUB),
        ({"name": "Luis\x00\nMora"}, "LuisMora"),
        ({"name": "x" * 200}, "x" * 80),
    ],
)
def test_el_nombre_es_el_name_o_el_correo_o_el_sub(carga: dict[str, Any], esperado: str) -> None:
    assert nombre_de_analista(SUB, _token(carga)) == esperado


@pytest.mark.parametrize(
    "autorizacion",
    [
        None,
        "",
        "Bearer",
        "Bearer a.b",
        "Bearer a.%%%.c",
        "Bearer a.bm8tanNvbg.c",
        "Bearer a.WzFd.c",
    ],
)
def test_sin_un_token_legible_el_nombre_es_el_sub(autorizacion: str | None) -> None:
    assert nombre_de_analista(SUB, autorizacion) == SUB


# --- Aviso de acceso denegado (DS3-09) --------------------------------------------------------------
@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _pedir_asincrono(metodo: str, ruta_: str, encabezados: dict[str, str]) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bff"
    ) as cliente:
        respuesta = await cliente.request(metodo, ruta_, headers=encabezados)
    await esperar_avisos()
    return respuesta


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("metodo", "ruta_", "recurso", "recurso_id"),
    [
        ("GET", "/v1/siniestros", "BANDEJA", None),
        (
            "GET",
            "/v1/siniestros/resumen?desde=2026-11-05T05:00:00Z&hasta=2026-11-06T05:00:00Z",
            "RESUMEN",
            None,
        ),
        ("POST", "/v1/siniestros/siguiente", "SIGUIENTE", None),
        ("GET", f"/v1/siniestros/{SINIESTRO_ID}", "SINIESTRO", SINIESTRO_ID),
        (
            "POST",
            f"/v1/siniestros/{SINIESTRO_ID}/evidencias/{EVIDENCIA_ID}/enlace",
            "EVIDENCIA",
            SINIESTRO_ID,
        ),
    ],
)
async def test_un_usuario_sin_el_grupo_se_avisa_a_siniestros(
    simulado: Simulado, metodo: str, ruta_: str, recurso: str, recurso_id: str | None
) -> None:
    respuesta = await _pedir_asincrono(metodo, ruta_, _usuario("[asesores]", "sub-asesor", **IDEM))
    assert respuesta.status_code == 403
    [aviso] = simulado.avisos
    assert aviso.url.path == "/v1/siniestros/accesos-denegados"
    assert json.loads(aviso.content) == {
        "actorId": "sub-asesor",
        "recurso": recurso,
        "recursoId": recurso_id,
        "motivo": "SIN_GRUPO",
    }
    assert aviso.headers["X-Actor-Id"] == "sub-asesor"
    assert aviso.headers["X-Correlation-Id"] == "corr-bff-w19"


@pytest.mark.anyio
async def test_el_aviso_no_demora_la_respuesta(simulado: Simulado) -> None:
    liberar = asyncio.Event()

    async def lento(pedido: httpx.Request) -> httpx.Response:
        await liberar.wait()
        return httpx.Response(202)

    servicio = ruta.siniestros_servicio()
    servicio.siniestros.transporte = httpx.MockTransport(lento)  # type: ignore[arg-type,union-attr]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bff"
    ) as cliente:
        respuesta = await asyncio.wait_for(
            cliente.get("/v1/siniestros", headers=_usuario("[asesores]", "sub-asesor")), timeout=2
        )
        assert respuesta.status_code == 403
        liberar.set()
    await esperar_avisos()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "falla", [_json(500, {}), _json(403, {}), _lanza(httpx.ReadTimeout), _lanza(httpx.ConnectError)]
)
async def test_si_el_aviso_falla_deja_el_log_sin_datos_personales(
    simulado: Simulado, caplog: pytest.LogCaptureFixture, falla: Any
) -> None:
    simulado.responder = falla
    with caplog.at_level(logging.WARNING):
        respuesta = await _pedir_asincrono(
            "GET", "/v1/siniestros", _usuario("[asesores]", "sub-asesor")
        )
    assert respuesta.status_code == 403
    [registro] = [r for r in caplog.records if r.getMessage() == "acceso.denegado"]
    assert (registro.recurso, registro.motivo) == ("BANDEJA", "SIN_GRUPO")  # type: ignore[attr-defined]
    assert hasattr(registro, "error_type")
    assert "sub-asesor" not in f"{registro.getMessage()} {registro.__dict__}"


@pytest.mark.anyio
async def test_sin_usuario_no_hay_a_quien_atribuir_el_aviso_y_solo_queda_el_log(
    simulado: Simulado, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        respuesta = await _pedir_asincrono(
            "GET", "/v1/siniestros", _usuario("[asesores]", sub=None)
        )
    assert respuesta.status_code == 403
    assert simulado.avisos == []
    assert any(r.getMessage() == "acceso.denegado" for r in caplog.records)


# --- HU-W20: la decisión ----------------------------------------------------------------------------
RUTA_DECISION = f"/v1/siniestros/{SINIESTRO_ID}/decision"


def _decidir(cuerpo: Any, clave: str = IDEM["Idempotency-Key"]) -> httpx.Response:
    return TestClient(app).post(
        RUTA_DECISION, json=cuerpo, headers={**ANALISTA, "Idempotency-Key": clave}
    )


@pytest.mark.parametrize("cuerpo", [APROBAR, RECHAZAR, SOPORTE])
def test_la_decision_se_reenvia_con_la_clave_el_actor_y_el_cuerpo(
    simulado: Simulado, cuerpo: dict[str, Any]
) -> None:
    respuesta = _decidir(cuerpo)
    assert respuesta.status_code == 200
    assert respuesta.json() == RESULTADO_APROBADO
    (pedido,) = [p for p in simulado.pedidos if p.url.path.endswith("/decision")]
    assert (pedido.method, pedido.url.path) == ("POST", RUTA_DECISION)
    assert pedido.headers["Idempotency-Key"] == IDEM["Idempotency-Key"]
    assert pedido.headers["X-Actor-Id"] == SUB
    assert pedido.headers["X-Actor-Rol"] == "ANALISTA_SINIESTROS"
    enviado = json.loads(pedido.content)
    assert enviado["tipo"] == cuerpo["tipo"] and enviado["justificacion"] == cuerpo["justificacion"]
    # Lo que no se envió (la lista de evidencias vacía) llega con su valor por omisión.
    assert enviado["evidenciasConsideradas"] == cuerpo.get("evidenciasConsideradas", [])


def test_el_nombre_del_analista_viaja_con_tildes_en_la_decision(simulado: Simulado) -> None:
    respuesta = TestClient(app).post(
        RUTA_DECISION,
        json=APROBAR,
        headers={
            **ANALISTA,
            **IDEM,
            "Authorization": _token({"name": "José Muñoz Peña"}),
        },
    )
    assert respuesta.status_code == 200
    (pedido,) = [p for p in simulado.pedidos if p.url.path.endswith("/decision")]
    assert pedido.headers["X-Actor-Nombre"] == "José Muñoz Peña"


@pytest.mark.parametrize(
    ("estado", "codigo", "detalles"),
    [
        (409, "TRANSICION_NO_PERMITIDA", {"estadoActual": "APROBADO"}),
        (409, "CASO_ASIGNADO_A_OTRO", {}),
        (404, "SINIESTRO_NO_ENCONTRADO", {}),
        (422, "JUSTIFICACION_REQUERIDA", {}),
        (422, "CAUSAL_INVALIDA", {}),
        (422, "VALOR_FUERA_DE_RANGO", {"campos": ["liquidacion.valorIndemnizar"]}),
        (422, "SOLICITUD_INVALIDA", {"campos": ["documentos"]}),
        (422, "IDEMPOTENCIA_CONFLICTO", {}),
    ],
)
def test_los_errores_de_la_decision_pasan_con_su_codigo_y_su_estado(
    simulado: Simulado, estado: int, codigo: str, detalles: dict[str, Any]
) -> None:
    simulado.responder = _json(
        estado, {"codigo": codigo, "mensaje": "m", "correlationId": "c", "detalles": detalles}
    )
    respuesta = _decidir(APROBAR)
    assert respuesta.status_code == estado
    assert set(respuesta.json()) == {"codigo", "mensaje", "correlationId", "detalles"}
    assert (respuesta.json()["codigo"], respuesta.json()["detalles"]) == (codigo, detalles)


@pytest.mark.parametrize(
    "responder",
    [
        _json(500, {"codigo": "ERROR_INTERNO"}),
        _json(401, {"codigo": "ACTOR_REQUERIDO"}),
        _json(403, {"codigo": "ROL_NO_AUTORIZADO"}),
        # Un 409 o un 422 con un código que el contrato no promete es falla cerrada.
        _json(409, {"codigo": "OTRO_CONFLICTO"}),
        _json(422, {"codigo": "OTRO"}),
        _json(200, {"estadoNuevo": "APROBADO"}),
        _json(200, {"siniestroId": "x"}),
        _lanza(httpx.ConnectError),
        _lanza(httpx.ReadTimeout),
    ],
)
def test_la_decision_falla_cerrada_con_cualquier_otra_respuesta(
    simulado: Simulado, responder: Callable[[httpx.Request], httpx.Response]
) -> None:
    simulado.responder = responder
    respuesta = _decidir(APROBAR)
    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "SERVICIO_NO_DISPONIBLE"
    assert respuesta.json()["detalles"] == {}


@pytest.mark.parametrize("grupos", ["[asesores]", "[suscriptores]", "[]", ""])
def test_solo_el_grupo_de_analistas_decide_y_se_avisa_a_siniestros(
    simulado: Simulado, grupos: str
) -> None:
    respuesta = TestClient(app).post(
        RUTA_DECISION, json=APROBAR, headers={**_usuario(grupos), **IDEM}
    )
    assert respuesta.status_code == 403
    assert [p for p in simulado.pedidos if p.url.path.endswith("/decision")] == []
    asyncio.run(esperar_avisos())
    (aviso,) = simulado.avisos
    cuerpo = json.loads(aviso.content)
    assert (cuerpo["recurso"], cuerpo["recursoId"], cuerpo["motivo"]) == (
        "SINIESTRO",
        SINIESTRO_ID,
        "SIN_GRUPO",
    )


@pytest.mark.parametrize("clave", [None, "no-es-uuid"])
def test_la_decision_exige_una_clave_uuid(simulado: Simulado, clave: str | None) -> None:
    encabezados = dict(ANALISTA)
    if clave:
        encabezados["Idempotency-Key"] = clave
    respuesta = TestClient(app).post(RUTA_DECISION, json=APROBAR, headers=encabezados)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (422, "SOLICITUD_INVALIDA")
    assert respuesta.json()["detalles"]["campos"] == ["Idempotency-Key"]
    assert [p for p in simulado.pedidos if p.url.path.endswith("/decision")] == []


@pytest.mark.parametrize(
    ("cuerpo", "campos"),
    [
        ({**SOPORTE, "documentos": []}, ["documentos"]),
        ({**SOPORTE, "canal": "SMS"}, ["canal"]),
        ({**SOPORTE, "plazoDiasHabiles": 7}, ["plazoDiasHabiles"]),
        ({**RECHAZAR, "liquidacion": {}}, ["liquidacion"]),
        ({"justificacion": "x"}, ["tipo"]),
        ({"tipo": "ANULAR", "justificacion": "x"}, ["tipo"]),
        ([1], ["tipo"]),
        (
            {**APROBAR, "liquidacion": {"valorReclamado": "1"}},
            [
                "liquidacion.deducible",
                "liquidacion.valorIndemnizar",
                "liquidacion.moneda",
            ],
        ),
    ],
)
def test_un_cuerpo_invalido_es_422_sin_llamar_a_siniestros_y_con_los_campos_limpios(
    simulado: Simulado, cuerpo: Any, campos: list[str]
) -> None:
    respuesta = _decidir(cuerpo)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (422, "SOLICITUD_INVALIDA")
    assert respuesta.json()["detalles"]["campos"] == campos
    assert [p for p in simulado.pedidos if p.url.path.endswith("/decision")] == []


def test_el_catalogo_se_reenvia_y_se_valida(simulado: Simulado) -> None:
    respuesta = _pedir("GET", "/v1/siniestros/catalogos-decision")
    assert respuesta.status_code == 200 and respuesta.json() == CATALOGOS
    (pedido,) = [p for p in simulado.pedidos if p.url.path.endswith("/catalogos-decision")]
    assert pedido.headers["X-Actor-Rol"] == "ANALISTA_SINIESTROS"
    simulado.responder = _json(200, {"causales": []})
    assert _pedir("GET", "/v1/siniestros/catalogos-decision").status_code == 503


def test_el_catalogo_solo_lo_ve_el_grupo_de_analistas(simulado: Simulado) -> None:
    respuesta = _pedir("GET", "/v1/siniestros/catalogos-decision", _usuario("[asesores]"))
    assert respuesta.status_code == 403
    asyncio.run(esperar_avisos())
    assert json.loads(simulado.avisos[0].content)["recurso"] == "SINIESTRO"


def test_ni_los_registros_ni_la_respuesta_llevan_los_textos_al_cliente(
    simulado: Simulado, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        respuestas = [_decidir(RECHAZAR), _decidir(SOPORTE)]
    registros = "\n".join(
        f"{r.getMessage()} {json.dumps({k: str(v) for k, v in r.__dict__.items()})}"
        for r in caplog.records
    )
    for texto in (EXPLICACION, MENSAJE, "Daño verificado", "La póliza había vencido"):
        assert texto not in registros
        assert all(texto not in r.text for r in respuestas)


def test_la_decision_se_mide_con_su_operacion(
    simulado: Simulado, caplog: pytest.LogCaptureFixture
) -> None:
    simulado.responder = _json(
        409, {"codigo": "TRANSICION_NO_PERMITIDA", "mensaje": "m", "detalles": {}}
    )
    with caplog.at_level(logging.INFO):
        _decidir(APROBAR)
    medida = next(r for r in caplog.records if getattr(r, "operation", None) == "siniestro.decidir")
    assert medida.result == "rechazado"  # type: ignore[attr-defined]
