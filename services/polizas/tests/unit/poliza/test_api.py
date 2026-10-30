"""Las rutas de la póliza con los dobles (HU-W17, 17.9; ADR 0014, decisiones 4 y 5):
GET /v1/cotizaciones/{cotizacionId}/poliza, GET /v1/polizas/{polizaId}/documento y
POST /v1/polizas/verificaciones. Mismos actores, encabezados y errores que GET …/aceptacion."""

import dataclasses
import io
import json
from collections.abc import Callable, Iterator
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from polizas import main
from polizas.config import Settings
from polizas.domain.poliza.errores import FalloAlmacenDocumentos
from polizas.domain.poliza.modelos import Poliza
from polizas.domain.suscripcion.insumos import texto

ASESOR = {"X-Actor-Id": "sub-asesor-1", "X-Actor-Rol": "ASESOR"}
SUSCRIPTOR = {"X-Actor-Id": "sub-suscriptor-1", "X-Actor-Rol": "SUSCRIPTOR"}
TODAS = {
    "leidoCondiciones": True,
    "informacionVeraz": True,
    "debitoAutomatico": True,
    "avisosElectronicos": False,
}
HUELLA = "sha256:" + "ab" * 32
OTRA_HUELLA = "sha256:" + "cd" * 32
FIRMA_URL = "X-Amz-Signature=0123456789abcdef"
URL = f"https://polizas.s3.amazonaws.com/polizas/x.pdf?versionId=v-registrada&{FIRMA_URL}"
VERIFICACIONES = "/v1/polizas/verificaciones"


class AlmacenFalso:
    def __init__(self) -> None:
        self.falla: Exception | None = None
        self.pedidas: list[tuple[str, str, int]] = []

    async def guardar(self, clave: str, contenido: bytes) -> str:
        raise AssertionError("la lectura no sube documentos")

    async def url_descarga(self, clave: str, version_id: str, vence_s: int) -> str:
        self.pedidas.append((clave, version_id, vence_s))
        if self.falla is not None:
            raise self.falla
        return URL


@pytest.fixture
def almacen() -> AlmacenFalso:
    return AlmacenFalso()


@pytest.fixture
def abrir(
    monkeypatch: pytest.MonkeyPatch,
    dobles: Any,
    almacen: AlmacenFalso,
    capturar_logs: Callable[..., io.StringIO],
) -> Callable[..., Any]:
    """La app con los dobles y el almacén falso (o sin almacén); devuelve el cliente y los logs."""

    def abrir(con_almacen: bool = True, **settings: Any) -> Any:
        monkeypatch.setattr(main, "almacen_desde", lambda s: almacen if con_almacen else None)
        app = main.crear_app(Settings(**settings), puertos=dobles.puertos())
        return TestClient(app), capturar_logs()

    return abrir


@pytest.fixture
def api(abrir: Callable[..., Any]) -> Iterator[TestClient]:
    cliente, _ = abrir()
    with cliente:
        yield cliente


def _aceptada(api: TestClient, sinteticos: Any, dobles: Any) -> Any:
    """La aceptación de S01 por la API; devuelve la del doble (dominio)."""
    decision = api.post(
        "/v1/suscripciones",
        json={"cotizacionId": sinteticos.cotizacion_id("S01")},
        headers={**ASESOR, "Idempotency-Key": str(uuid4())},
    ).json()
    respuesta = api.post(
        f"/v1/cotizaciones/{decision['cotizacionId']}/aceptacion",
        json={
            "decisionId": decision["id"],
            "versionDeclaraciones": "2026.10.1",
            "declaraciones": TODAS,
        },
        headers={**ASESOR, "Idempotency-Key": str(uuid4())},
    )
    assert respuesta.status_code == 201, respuesta.json()
    return next(a for a in dobles.aceptaciones.aceptaciones if str(a.id) == respuesta.json()["id"])


def _otra(aceptacion: Any) -> Any:
    """Otra aceptación (solo S01 sale APROBADA y se acepta una vez): para una segunda póliza."""
    return dataclasses.replace(aceptacion, id=uuid4(), cotizacion_id=uuid4())


def _ruta(cotizacion_id: Any) -> str:
    return f"/v1/cotizaciones/{cotizacion_id}/poliza"


def _documento(poliza_id: Any) -> str:
    return f"/v1/polizas/{poliza_id}/documento"


def _cuerpo_base(poliza: Poliza, aceptacion: Any, sinteticos: Any) -> dict[str, Any]:
    insumos = sinteticos.insumos("S01")
    return {
        "polizaId": str(poliza.poliza_id),
        "estado": poliza.estado.value,
        "numero": poliza.numero,
        "producto": {"id": "vida-hipotecario", "version": insumos.version_producto},
        "ramo": {"codigo": "100", "nombre": "Vida hipotecario"},
        "coberturas": [
            {"codigo": c.codigo, "nombre": c.nombre, "obligatoria": c.obligatoria}
            for c in insumos.coberturas
        ],
        "vigencia": {
            "desde": poliza.vigencia.desde.isoformat(),
            "hasta": poliza.vigencia.hasta.isoformat(),
        },
        "sumaAsegurada": texto(insumos.suma_asegurada),
        "prima": texto(insumos.prima_total),
        "moneda": "COP",
        "periodicidad": insumos.periodicidad,
        "aceptadaEn": aceptacion.aceptada_en.isoformat().replace("+00:00", "Z"),
        "recaudadaEn": "2026-10-22T15:00:01.250000Z",
    }


# --- GET /v1/cotizaciones/{cotizacionId}/poliza ----------------------------------------------
def test_sin_aceptacion_404(api: TestClient) -> None:
    respuesta = api.get(_ruta(uuid4()), headers={**ASESOR, "X-Correlation-Id": "corr-poliza-1"})
    assert respuesta.status_code == 404
    assert respuesta.json() == {
        "codigo": "ACEPTACION_NO_ENCONTRADA",
        "mensaje": "La aceptación no existe",
        "correlationId": "corr-poliza-1",
        "detalles": {},
    }


def test_aceptada_sin_poliza_404_poliza_no_encontrada(
    api: TestClient, sinteticos: Any, dobles: Any
) -> None:
    """AC2: el recaudo está pendiente o fue rechazado, o el producto no tiene ramo."""
    aceptacion = _aceptada(api, sinteticos, dobles)
    respuesta = api.get(
        _ruta(aceptacion.cotizacion_id), headers={**ASESOR, "X-Correlation-Id": "corr-poliza-2"}
    )
    assert respuesta.status_code == 404
    assert respuesta.json() == {
        "codigo": "POLIZA_NO_ENCONTRADA",
        "mensaje": "La póliza no existe",
        "correlationId": "corr-poliza-2",
        "detalles": {},
    }


def test_emitiendo_sin_documento_ni_firma_ni_emitida_en(
    api: TestClient, sinteticos: Any, dobles: Any, registrar_poliza: Callable[..., Poliza]
) -> None:
    aceptacion = _aceptada(api, sinteticos, dobles)
    poliza = registrar_poliza(aceptacion, "EMITIENDO")
    respuesta = api.get(_ruta(aceptacion.cotizacion_id), headers=ASESOR)
    assert respuesta.status_code == 200
    assert respuesta.json() == {
        **_cuerpo_base(poliza, aceptacion, sinteticos),
        "estado": "EMITIENDO",
        "emitidaEn": None,
        "documento": None,
        "firma": None,
    }


def test_emitiendo_con_los_puntos_de_control_ya_registrados(
    api: TestClient, sinteticos: Any, dobles: Any, registrar_poliza: Callable[..., Poliza]
) -> None:
    aceptacion = _aceptada(api, sinteticos, dobles)
    registrar_poliza(aceptacion, "FIRMADA")
    cuerpo = api.get(_ruta(aceptacion.cotizacion_id), headers=ASESOR).json()
    assert (cuerpo["estado"], cuerpo["emitidaEn"]) == ("EMITIENDO", None)
    assert cuerpo["documento"] == {"huella": HUELLA, "versionId": "v-registrada"}
    assert cuerpo["firma"] == {"firmaId": "FIR-SINT-C01", "selloTiempo": "2026-01-01T00:00:00Z"}


def test_activa_con_su_documento_y_su_firma(
    api: TestClient, sinteticos: Any, dobles: Any, registrar_poliza: Callable[..., Poliza]
) -> None:
    aceptacion = _aceptada(api, sinteticos, dobles)
    poliza = registrar_poliza(aceptacion)
    respuesta = api.get(_ruta(aceptacion.cotizacion_id), headers=SUSCRIPTOR)
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo == {
        **_cuerpo_base(poliza, aceptacion, sinteticos),
        "estado": "ACTIVA",
        "emitidaEn": "2026-10-22T15:00:00Z",
        "documento": {"huella": HUELLA, "versionId": "v-registrada"},
        "firma": {"firmaId": "FIR-SINT-C01", "selloTiempo": "2026-01-01T00:00:00Z"},
    }
    # Lo interno no sale: ni la clave del objeto, ni el fallo, ni la reserva, ni pasos.
    texto_cuerpo = json.dumps(cuerpo)
    assert "polizas/" not in texto_cuerpo
    assert not {"clave", "ultimoFallo", "reservadaHasta", "pasos", "clienteId"} & set(cuerpo)


# --- GET /v1/polizas/{polizaId}/documento -----------------------------------------------------
def test_documento_de_la_version_registrada(
    api: TestClient,
    sinteticos: Any,
    dobles: Any,
    registrar_poliza: Callable[..., Poliza],
    almacen: AlmacenFalso,
) -> None:
    aceptacion = _aceptada(api, sinteticos, dobles)
    poliza = registrar_poliza(aceptacion)
    respuesta = api.get(_documento(poliza.poliza_id), headers=ASESOR)
    assert respuesta.status_code == 200
    # El vencimiento configurado (300 s por defecto), desde el reloj.
    assert respuesta.json() == {"url": URL, "venceEn": "2026-10-22T15:05:00Z"}
    assert poliza.documento is not None
    assert almacen.pedidas == [(poliza.documento.clave, "v-registrada", 300)]


def test_documento_con_otro_vencimiento_configurado(
    abrir: Callable[..., Any],
    sinteticos: Any,
    dobles: Any,
    registrar_poliza: Callable[..., Poliza],
    almacen: AlmacenFalso,
) -> None:
    cliente, _ = abrir(documento_url_vence_s=60)
    with cliente as api:
        poliza = registrar_poliza(_aceptada(api, sinteticos, dobles))
        respuesta = api.get(_documento(poliza.poliza_id), headers=ASESOR)
    assert respuesta.json()["venceEn"] == "2026-10-22T15:01:00Z"
    assert [vence for _, _, vence in almacen.pedidas] == [60]


def test_documento_404(api: TestClient) -> None:
    respuesta = api.get(_documento(uuid4()), headers={**ASESOR, "X-Correlation-Id": "c-doc"})
    assert respuesta.status_code == 404
    assert respuesta.json() == {
        "codigo": "POLIZA_NO_ENCONTRADA",
        "mensaje": "La póliza no existe",
        "correlationId": "c-doc",
        "detalles": {},
    }


def test_documento_409_mientras_se_emite(
    api: TestClient,
    sinteticos: Any,
    dobles: Any,
    registrar_poliza: Callable[..., Poliza],
    almacen: AlmacenFalso,
) -> None:
    poliza = registrar_poliza(_aceptada(api, sinteticos, dobles), "FIRMADA")
    respuesta = api.get(_documento(poliza.poliza_id), headers={**ASESOR, "X-Correlation-Id": "c"})
    assert respuesta.status_code == 409
    assert respuesta.json() == {
        "codigo": "POLIZA_NO_ACTIVA",
        "mensaje": "La póliza todavía se está emitiendo",
        "correlationId": "c",
        "detalles": {},
    }
    assert almacen.pedidas == []


def test_documento_503_sin_almacen(
    abrir: Callable[..., Any], sinteticos: Any, dobles: Any, registrar_poliza: Callable[..., Poliza]
) -> None:
    cliente, _ = abrir(con_almacen=False)
    with cliente as api:
        poliza = registrar_poliza(_aceptada(api, sinteticos, dobles))
        respuesta = api.get(_documento(poliza.poliza_id), headers=ASESOR)
        # Sin almacén, una póliza inexistente sigue siendo 404.
        inexistente = api.get(_documento(uuid4()), headers=ASESOR)
    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "DEPENDENCIA_NO_DISPONIBLE"
    assert respuesta.json()["detalles"] == {"dependencia": "documentos"}
    assert inexistente.status_code == 404


def test_documento_503_si_falla_la_firma_de_la_url(
    api: TestClient,
    sinteticos: Any,
    dobles: Any,
    registrar_poliza: Callable[..., Poliza],
    almacen: AlmacenFalso,
) -> None:
    poliza = registrar_poliza(_aceptada(api, sinteticos, dobles))
    almacen.falla = FalloAlmacenDocumentos(FalloAlmacenDocumentos.NO_DISPONIBLE)
    respuesta = api.get(_documento(poliza.poliza_id), headers=ASESOR)
    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "DEPENDENCIA_NO_DISPONIBLE"
    assert respuesta.json()["detalles"] == {"dependencia": "documentos"}


# --- POST /v1/polizas/verificaciones ----------------------------------------------------------
def test_verificacion_valida(
    api: TestClient, sinteticos: Any, dobles: Any, registrar_poliza: Callable[..., Poliza]
) -> None:
    poliza = registrar_poliza(_aceptada(api, sinteticos, dobles))
    respuesta = api.post(
        VERIFICACIONES, json={"numero": poliza.numero, "huella": HUELLA}, headers=ASESOR
    )
    assert respuesta.status_code == 200
    assert respuesta.json() == {
        "valido": True,
        "numero": poliza.numero,
        "firmaId": "FIR-SINT-C01",
        "selloTiempo": "2026-01-01T00:00:00Z",
    }


def test_las_tres_no_validas_tienen_el_mismo_cuerpo_salvo_el_numero(
    api: TestClient, sinteticos: Any, dobles: Any, registrar_poliza: Callable[..., Poliza]
) -> None:
    """Huella distinta, número inexistente y póliza EMITIENDO: no revela si el número existe."""
    aceptacion = _aceptada(api, sinteticos, dobles)
    activa = registrar_poliza(aceptacion)
    emitiendo = registrar_poliza(_otra(aceptacion), "FIRMADA", secuencia=8)
    casos = [
        (activa.numero, OTRA_HUELLA),
        ("100-2026-999999", HUELLA),
        (emitiendo.numero, HUELLA),
    ]
    respuestas = [
        api.post(VERIFICACIONES, json={"numero": n, "huella": h}, headers=ASESOR) for n, h in casos
    ]
    assert {r.status_code for r in respuestas} == {200}
    cuerpos = [r.json() for r in respuestas]
    for (numero, _), cuerpo in zip(casos, cuerpos, strict=True):
        assert cuerpo == {"valido": False, "numero": numero, "firmaId": None, "selloTiempo": None}
    # Byte a byte, salvo el número; y los mismos encabezados de contenido.
    sin_numero = {r.content.replace(n.encode(), b"<n>") for r, (n, _) in zip(respuestas, casos)}
    assert len(sin_numero) == 1
    assert len({r.headers["content-type"] for r in respuestas}) == 1


@pytest.mark.parametrize(
    ("cuerpo", "campos"),
    [
        ({"numero": "100-2026-1", "huella": HUELLA}, ["numero"]),
        ({"numero": "POL-2026-000001", "huella": HUELLA}, ["numero"]),
        ({"numero": "100-2026-000001", "huella": "ab" * 32}, ["huella"]),
        ({"numero": "100-2026-000001", "huella": HUELLA.upper()}, ["huella"]),
        ({"numero": "100-2026-000001", "huella": "sha256:" + "a" * 63}, ["huella"]),
        ({"numero": "100-2026-000001"}, ["huella"]),
        ({"huella": HUELLA}, ["numero"]),
        ({"numero": 100, "huella": HUELLA}, ["numero"]),
    ],
    ids=[
        "numero_corto",
        "numero_otro_formato",
        "huella_sin_prefijo",
        "huella_mayusculas",
        "huella_corta",
        "sin_huella",
        "sin_numero",
        "numero_no_texto",
    ],
)
def test_verificacion_con_formato_invalido_422(
    api: TestClient, cuerpo: dict[str, Any], campos: list[str]
) -> None:
    respuesta = api.post(VERIFICACIONES, json=cuerpo, headers=ASESOR)
    assert respuesta.status_code == 422
    assert respuesta.json()["codigo"] == "SOLICITUD_INVALIDA"
    assert [c.rsplit(".", 1)[-1] for c in respuesta.json()["detalles"]["campos"]] == campos


def test_verificacion_sin_cuerpo_422(api: TestClient) -> None:
    respuesta = api.post(VERIFICACIONES, headers=ASESOR)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (422, "SOLICITUD_INVALIDA")


# --- Actores, encabezados y configuración -----------------------------------------------------
def test_actores_y_encabezados_en_las_tres_rutas(
    api: TestClient, sinteticos: Any, dobles: Any, registrar_poliza: Callable[..., Poliza]
) -> None:
    aceptacion = _aceptada(api, sinteticos, dobles)
    poliza = registrar_poliza(aceptacion)
    verificacion = {"numero": poliza.numero, "huella": HUELLA}
    pedidos: list[Callable[[dict[str, str]], Any]] = [
        lambda h: api.get(_ruta(aceptacion.cotizacion_id), headers=h),
        lambda h: api.get(_documento(poliza.poliza_id), headers=h),
        lambda h: api.post(VERIFICACIONES, json=verificacion, headers=h),
    ]
    for pedir in pedidos:
        # Los mismos roles que GET …/aceptacion: ASESOR y SUSCRIPTOR leen.
        assert pedir(ASESOR).status_code == 200
        assert pedir(SUSCRIPTOR).status_code == 200
        otro = pedir({"X-Actor-Id": "a", "X-Actor-Rol": "CLIENTE"})
        assert (otro.status_code, otro.json()["codigo"]) == (403, "ROL_NO_AUTORIZADO")
        sin_actor = pedir({})
        assert (sin_actor.status_code, sin_actor.json()["codigo"]) == (401, "ACTOR_REQUERIDO")
        assert pedir({"X-Actor-Id": "a"}).status_code == 401
        correlacion = pedir({**ASESOR, "X-Correlation-Id": "corr-poliza-x"})
        assert correlacion.headers["X-Correlation-Id"] == "corr-poliza-x"
    # El actor va antes que el cuerpo: sin actor y con un cuerpo inválido, 401.
    assert api.post(VERIFICACIONES, json={"numero": "x"}).status_code == 401
    for ruta in (_ruta("no-es-uuid"), _documento("no-es-uuid")):
        invalida = api.get(ruta, headers=ASESOR)
        assert (invalida.status_code, invalida.json()["codigo"]) == (422, "SOLICITUD_INVALIDA")


def test_sin_puertos_las_tres_rutas_responden_503() -> None:
    with TestClient(main.crear_app(Settings())) as cliente:
        respuestas = [
            cliente.get(_ruta(uuid4()), headers=ASESOR),
            cliente.get(_documento(uuid4()), headers=ASESOR),
            cliente.post(
                VERIFICACIONES,
                json={"numero": "100-2026-000001", "huella": HUELLA},
                headers=ASESOR,
            ),
        ]
    for respuesta in respuestas:
        assert (respuesta.status_code, respuesta.json()["codigo"]) == (
            503,
            "DEPENDENCIA_NO_DISPONIBLE",
        )


def test_puertos_sin_la_lectura_de_la_poliza_responden_503(dobles: Any) -> None:
    # Arneses anteriores a la 17.9 construyen los puertos sin `polizas`.
    from polizas.main import Puertos

    puertos = Puertos(
        dobles.insumos,
        dobles.reglas,
        dobles.decisiones,
        dobles.codigos,
        dobles.reloj,
        dobles.aceptaciones,
        dobles.recaudos,
    )
    with TestClient(main.crear_app(Settings(), puertos=puertos)) as cliente:
        respuesta = cliente.get(_ruta(uuid4()), headers=ASESOR)
        recaudo = cliente.get(f"/v1/cotizaciones/{uuid4()}/recaudo", headers=ASESOR)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (
        503,
        "DEPENDENCIA_NO_DISPONIBLE",
    )
    # El resto de la API sigue igual.
    assert recaudo.status_code == 404


# --- Logs -------------------------------------------------------------------------------------
def test_logs_con_una_operacion_por_ruta_sin_la_url_ni_pii(
    abrir: Callable[..., Any],
    sinteticos: Any,
    dobles: Any,
    registrar_poliza: Callable[..., Poliza],
    almacen: AlmacenFalso,
) -> None:
    cliente, salida = abrir()
    with cliente as api:
        aceptacion = _aceptada(api, sinteticos, dobles)
        poliza = registrar_poliza(aceptacion)
        emitiendo = registrar_poliza(_otra(aceptacion), "FIRMADA", secuencia=8)
        api.get(_ruta(aceptacion.cotizacion_id), headers=ASESOR)
        api.get(_ruta(uuid4()), headers=ASESOR)
        api.get(_documento(poliza.poliza_id), headers=ASESOR)
        api.get(_documento(emitiendo.poliza_id), headers=ASESOR)
        almacen.falla = FalloAlmacenDocumentos(FalloAlmacenDocumentos.NO_DISPONIBLE, "SlowDown")
        api.get(_documento(poliza.poliza_id), headers=ASESOR)
        for huella in (HUELLA, OTRA_HUELLA):
            api.post(
                VERIFICACIONES, json={"numero": poliza.numero, "huella": huella}, headers=ASESOR
            )
    logs = [json.loads(linea) for linea in salida.getvalue().splitlines() if linea.strip()]
    resultados = [
        (log["operation"], log["result"])
        for log in logs
        if (log.get("operation") or "").startswith("poliza.")
        and log["message"].startswith("operación ")
    ]
    assert resultados == [
        ("poliza.consultar", "ok"),
        ("poliza.consultar", "rechazado"),
        ("poliza.documento", "ok"),
        ("poliza.documento", "rechazado"),
        ("poliza.documento", "error"),
        ("poliza.verificar", "valida"),
        ("poliza.verificar", "no_valida"),
    ]
    texto_logs = salida.getvalue()
    # Ni la URL (ni su firma), ni el cliente, ni la huella.
    assert URL not in texto_logs and FIRMA_URL not in texto_logs
    assert str(aceptacion.cliente_id) not in texto_logs
    for huella in (HUELLA, OTRA_HUELLA):
        assert huella[7:] not in texto_logs
