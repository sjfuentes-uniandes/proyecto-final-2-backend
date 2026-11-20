import asyncio
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx
from bff_movil.adapters.inbound.http.siniestros import SolicitudBorrador
from bff_movil.adapters.outbound.siniestros_http import SiniestrosHttp
from bff_movil.application.siniestros.casos import CrearBorrador
from bff_movil.domain.polizas.modelos import Cobertura, Poliza, Vigencia
from bff_movil.domain.sesion.errores import ServicioNoDisponible
from bff_movil.domain.siniestros.errores import PolizaNoVigente
from pydantic import ValidationError

pytestmark = pytest.mark.anyio

SUB = "sub-cliente"
CLIENTE_ID = UUID("00000000-0000-4000-8000-000000000001")
POLIZA_ID = UUID("00000000-0000-4000-8000-000000000002")
CLAVE = UUID("00000000-0000-4000-8000-000000000003")


def poliza(**cambios: Any) -> Poliza:
    datos: dict[str, Any] = {
        "poliza_id": POLIZA_ID,
        "numero": "POL-1",
        "estado": "ACTIVA",
        "producto_nombre": "Producto",
        "ramo": "AUTO",
        "vigencia": Vigencia(date(2026, 1, 1), date(2026, 12, 31)),
        "vigente": True,
        "prima": Decimal("100.00"),
        "suma_asegurada": Decimal("10000.00"),
        "moneda": "COP",
        "periodicidad": "MENSUAL",
        "coberturas": (Cobertura("COLISION", "Colisión", True),),
        "documento_huella": "sha256:abc",
        "firma_id": "FIRMA-1",
        "emitida_en": datetime(2026, 1, 1, tzinfo=UTC),
    }
    return Poliza(**{**datos, **cambios})


class Resolutor:
    async def resolver(self, sub: str) -> UUID:
        assert sub == SUB
        return CLIENTE_ID


class Consulta:
    def __init__(self, poliza_consultada: Poliza) -> None:
        self.poliza = poliza_consultada
        self.llamadas: list[tuple[UUID, UUID, str]] = []

    async def obtener(self, cliente_id: UUID, poliza_id: UUID, actor_id: str) -> Poliza:
        self.llamadas.append((cliente_id, poliza_id, actor_id))
        return self.poliza

    async def listar(self, cliente_id: UUID, actor_id: str) -> list[Poliza]:
        raise AssertionError("no se debe listar pólizas")


class Siniestros:
    def __init__(self) -> None:
        self.llamadas: list[tuple[str, UUID, Poliza, str, UUID]] = []

    async def crear_borrador(
        self, actor_id: str, cliente_id: UUID, poliza: Poliza, descripcion: str, clave: UUID
    ) -> dict[str, Any]:
        self.llamadas.append((actor_id, cliente_id, poliza, descripcion, clave))
        return {"polizaId": str(poliza.poliza_id)}

    async def limites(self, actor_id: str) -> dict[str, Any]:
        raise AssertionError("no se deben consultar límites")

    async def listar_borradores(self, actor_id: str) -> dict[str, Any]:
        raise AssertionError("no se deben listar borradores")

    async def consultar_borrador(self, actor_id: str, borrador_id: UUID) -> dict[str, Any]:
        raise AssertionError("no se debe consultar un borrador")

    async def agregar_evidencia(
        self, actor_id: str, borrador_id: UUID, solicitud: dict[str, Any]
    ) -> dict[str, Any]:
        raise AssertionError("no se debe agregar evidencia")

    async def confirmar_evidencia(
        self, actor_id: str, borrador_id: UUID, evidencia_id: UUID
    ) -> dict[str, Any]:
        raise AssertionError("no se debe confirmar evidencia")

    async def renovar_carga(
        self, actor_id: str, borrador_id: UUID, evidencia_id: UUID
    ) -> dict[str, Any]:
        raise AssertionError("no se debe renovar la carga")


async def test_crea_borrador_con_cliente_resuelto_y_poliza_vigente() -> None:
    consulta, siniestros = Consulta(poliza()), Siniestros()
    descripcion = "El vehículo recibió un impacto en la puerta."

    resultado = await CrearBorrador(Resolutor(), consulta, siniestros).ejecutar(
        SUB, POLIZA_ID, descripcion, CLAVE
    )

    assert resultado == {"polizaId": str(POLIZA_ID)}
    assert consulta.llamadas == [(CLIENTE_ID, POLIZA_ID, SUB)]
    assert siniestros.llamadas == [(SUB, CLIENTE_ID, poliza(), descripcion, CLAVE)]


def test_descripcion_del_borrador_se_normaliza_y_tiene_maximo_de_2000_caracteres() -> None:
    texto = "  El vehículo recibió un impacto.  "
    solicitud = SolicitudBorrador(polizaId=POLIZA_ID, descripcion=texto)
    assert solicitud.descripcion == texto.strip()
    assert len(SolicitudBorrador(polizaId=POLIZA_ID, descripcion="x" * 2000).descripcion) == 2000

    with pytest.raises(ValidationError):
        SolicitudBorrador(polizaId=POLIZA_ID, descripcion=" " * 4)
    with pytest.raises(ValidationError):
        SolicitudBorrador(polizaId=POLIZA_ID, descripcion="x" * 2001)


async def test_reenvia_la_descripcion_al_servicio_siniestros() -> None:
    recibidas: list[httpx.Request] = []

    def manejar(request: httpx.Request) -> httpx.Response:
        recibidas.append(request)
        return httpx.Response(
            201,
            json={
                "borradorId": str(CLAVE),
                "polizaId": str(POLIZA_ID),
                "estado": "ABIERTO",
            },
        )

    descripcion = "El vehículo recibió un impacto."
    async with httpx.AsyncClient(transport=httpx.MockTransport(manejar)) as cliente:
        await SiniestrosHttp(cliente, "http://siniestros.test", timeout_ms=500).crear_borrador(
            SUB, CLIENTE_ID, poliza(), descripcion, CLAVE
        )

    assert json.loads(recibidas[0].content)["descripcion"] == descripcion
    assert json.loads(recibidas[0].content)["copiaPoliza"]["estado"] == "ACTIVA"


async def test_consulta_borradores_confirma_y_renueva_por_el_actor() -> None:
    recibidas: list[httpx.Request] = []

    def manejar(request: httpx.Request) -> httpx.Response:
        recibidas.append(request)
        return httpx.Response(200, json={"items": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(manejar)) as cliente:
        api = SiniestrosHttp(cliente, "http://siniestros.test", timeout_ms=500)
        await api.listar_borradores(SUB)
        await api.consultar_borrador(SUB, CLAVE)
        await api.confirmar_evidencia(SUB, CLAVE, POLIZA_ID)
        await api.renovar_carga(SUB, CLAVE, POLIZA_ID)

    assert [(request.method, request.url.path) for request in recibidas] == [
        ("GET", "/v1/siniestros/borradores"),
        ("GET", f"/v1/siniestros/borradores/{CLAVE}"),
        ("POST", f"/v1/siniestros/borradores/{CLAVE}/evidencias/{POLIZA_ID}/confirmacion"),
        ("POST", f"/v1/siniestros/borradores/{CLAVE}/evidencias/{POLIZA_ID}/carga"),
    ]
    assert all(request.headers["X-Actor-Id"] == SUB for request in recibidas)
    assert all(request.headers["X-Actor-Rol"] == "CLIENTE" for request in recibidas)


async def test_confirmar_y_enviar_tienen_presupuesto_propio_sin_ampliar_consultas() -> None:
    async def lento(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.8)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(lento)) as cliente:
        api = SiniestrosHttp(cliente, "http://siniestros.test", timeout_ms=500)
        await api.confirmar_evidencia(SUB, CLAVE, POLIZA_ID)
        await api.enviar_borrador(SUB, CLAVE, CLAVE, poliza())
        with pytest.raises(ServicioNoDisponible):
            await api.consultar_borrador(SUB, CLAVE)


@pytest.mark.parametrize(
    "datos",
    [
        {"estado": "CANCELADA", "vigente": True},
        {"estado": "EMITIDA", "vigente": True},
        {"estado": "ACTIVA", "vigente": False},
    ],
)
async def test_no_crea_borrador_para_poliza_no_vigente(datos: dict[str, Any]) -> None:
    consulta, siniestros = Consulta(poliza(**datos)), Siniestros()

    with pytest.raises(PolizaNoVigente):
        await CrearBorrador(Resolutor(), consulta, siniestros).ejecutar(
            SUB, POLIZA_ID, "Descripción del siniestro", CLAVE
        )

    assert consulta.llamadas == [(CLIENTE_ID, POLIZA_ID, SUB)]
    assert siniestros.llamadas == []


@pytest.mark.parametrize(
    ("estado", "vigente", "http"),
    [
        ("ACTIVA", True, 201),
        ("ACTIVA", False, 409),
        ("EMITIDA", True, 409),
        ("CANCELADA", True, 409),
    ],
)
def test_creacion_http_segun_estado_y_vigencia(
    crear_entorno: Callable[..., Any], estado: str, vigente: bool, http: int
) -> None:
    consulta = Consulta(poliza(estado=estado, vigente=vigente))
    entorno = crear_entorno(consulta_polizas=consulta, SINIESTROS_URL="http://siniestros.test")
    creado = {"borradorId": str(CLAVE), "polizaId": str(POLIZA_ID), "estado": "ABIERTO"}
    with respx.mock(assert_all_called=False) as router:
        servicio = router.post("http://siniestros.test/v1/siniestros/borradores").respond(
            201, json=creado
        )
        respuesta = entorno.pedir(
            "POST",
            "/v1/siniestros/borradores",
            cuerpo={"polizaId": str(POLIZA_ID), "descripcion": "Impacto en la puerta."},
            encabezados={"Idempotency-Key": str(CLAVE)},
        )

    assert respuesta.status_code == http, respuesta.text
    assert consulta.llamadas == [(CLIENTE_ID, POLIZA_ID, entorno.sub)]
    if http == 201:
        assert respuesta.json() == creado
        assert json.loads(servicio.calls[0].request.content)["copiaPoliza"]["estado"] == "ACTIVA"
    else:
        assert respuesta.json()["codigo"] == "POLIZA_NO_VIGENTE"
        assert not servicio.called


def test_creacion_http_rechaza_poliza_de_otro_titular(
    crear_entorno: Callable[..., Any],
) -> None:
    entorno = crear_entorno(
        CONSULTA_URL="http://consulta.test", SINIESTROS_URL="http://siniestros.test"
    )
    with respx.mock(assert_all_called=False) as router:
        consulta = router.get(
            f"http://consulta.test/v1/clientes/{CLIENTE_ID}/polizas/{POLIZA_ID}"
        ).respond(403, json={"codigo": "POLIZA_NO_AUTORIZADA"})
        servicio = router.post("http://siniestros.test/v1/siniestros/borradores")
        respuesta = entorno.pedir(
            "POST",
            "/v1/siniestros/borradores",
            cuerpo={"polizaId": str(POLIZA_ID), "descripcion": "Impacto en la puerta."},
            encabezados={"Idempotency-Key": str(CLAVE)},
        )

    assert respuesta.status_code == 403, respuesta.text
    assert respuesta.json()["codigo"] == "POLIZA_NO_AUTORIZADA"
    assert consulta.calls[0].request.headers["X-Actor-Id"] == entorno.sub
    assert not servicio.called


ENVIO = {
    "borradorId": str(CLAVE),
    "siniestroId": str(POLIZA_ID),
    "codigo": "88-2026-1000",
    "estado": "REPORTADO",
    "reportadoEn": "2026-11-18T15:00:00Z",
}
BORRADOR = {
    "borradorId": str(CLAVE),
    "polizaId": str(POLIZA_ID),
    "descripcion": "Impacto.",
    "estado": "ABIERTO",
    "creadoEn": "2026-11-18T14:00:00Z",
    "cantidadEvidencias": 0,
    "evidenciasPendientes": 0,
    "version": 1,
    "evidencias": [],
}
RUTA_BORRADOR = f"/v1/siniestros/borradores/{CLAVE}"


def test_envio_y_respuesta_perdida_recuperan_confirmacion(
    crear_entorno: Callable[..., Any],
) -> None:
    consulta = Consulta(poliza())
    entorno = crear_entorno(consulta_polizas=consulta, SINIESTROS_URL="http://siniestros.test")
    with respx.mock(assert_all_called=True) as router:
        lectura = router.get(f"http://siniestros.test{RUTA_BORRADOR}").respond(200, json=BORRADOR)
        envio = router.post(f"http://siniestros.test{RUTA_BORRADOR}/envio")
        # La primera respuesta se pierde despues de que el servicio confirma.
        envio.mock(
            side_effect=[httpx.ReadTimeout("respuesta perdida"), httpx.Response(201, json=ENVIO)]
        )
        encabezados = {"Idempotency-Key": str(CLAVE)}
        primera = entorno.pedir("POST", f"{RUTA_BORRADOR}/envio", encabezados=encabezados)
        assert primera.status_code == 503
        copia = json.loads(envio.calls[0].request.content)["copiaPoliza"]
        assert copia["estado"] == "ACTIVA" and copia["vigente"] is True
        lectura.respond(200, json={**BORRADOR, "estado": "CONVERTIDO", "envio": ENVIO})
        consulta.poliza = poliza(estado="VENCIDA", vigente=False)
        segunda = entorno.pedir("POST", f"{RUTA_BORRADOR}/envio", encabezados=encabezados)
    assert segunda.status_code == 201 and segunda.json() == ENVIO
    assert consulta.llamadas == [(CLIENTE_ID, POLIZA_ID, entorno.sub)]
    assert json.loads(envio.calls[1].request.content) == {"copiaPoliza": None}
    assert all(c.request.headers["X-Actor-Id"] == entorno.sub for c in envio.calls)
    assert all(c.request.headers["Idempotency-Key"] == str(CLAVE) for c in envio.calls)


def test_replay_convertido_no_depende_del_resolutor(crear_entorno: Callable[..., Any]) -> None:
    entorno = crear_entorno(con_cliente=False, SINIESTROS_URL="http://siniestros.test")
    with respx.mock as router:
        router.get(f"http://siniestros.test{RUTA_BORRADOR}").respond(
            200, json={**BORRADOR, "estado": "CONVERTIDO", "envio": ENVIO}
        )
        router.post(f"http://siniestros.test{RUTA_BORRADOR}/envio").respond(201, json=ENVIO)
        respuesta = entorno.pedir(
            "POST", f"{RUTA_BORRADOR}/envio", cuerpo={}, encabezados={"Idempotency-Key": str(CLAVE)}
        )
    assert respuesta.status_code == 201 and respuesta.json() == ENVIO


@pytest.mark.parametrize("estado,vigente", [("EMITIDA", True), ("ACTIVA", False)])
def test_envio_rechaza_poliza_invalida_sin_convertir(
    crear_entorno: Callable[..., Any], estado: str, vigente: bool
) -> None:
    entorno = crear_entorno(
        consulta_polizas=Consulta(poliza(estado=estado, vigente=vigente)),
        SINIESTROS_URL="http://siniestros.test",
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(f"http://siniestros.test{RUTA_BORRADOR}").respond(200, json=BORRADOR)
        envio = router.post(f"http://siniestros.test{RUTA_BORRADOR}/envio")
        respuesta = entorno.pedir(
            "POST", f"{RUTA_BORRADOR}/envio", encabezados={"Idempotency-Key": str(CLAVE)}
        )
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (409, "POLIZA_NO_VIGENTE")
    assert not envio.called


def test_borrador_ajeno_no_consulta_poliza_ni_envia(crear_entorno: Callable[..., Any]) -> None:
    consulta = Consulta(poliza())
    entorno = crear_entorno(consulta_polizas=consulta, SINIESTROS_URL="http://siniestros.test")
    with respx.mock(assert_all_called=False) as router:
        router.get(f"http://siniestros.test{RUTA_BORRADOR}").respond(
            404, json={"codigo": "BORRADOR_NO_ENCONTRADO", "mensaje": "No existe", "detalles": {}}
        )
        envio = router.post(f"http://siniestros.test{RUTA_BORRADOR}/envio")
        respuesta = entorno.pedir(
            "POST", f"{RUTA_BORRADOR}/envio", encabezados={"Idempotency-Key": str(CLAVE)}
        )
    assert respuesta.status_code == 404
    assert not envio.called and not consulta.llamadas


def test_actualiza_mismo_borrador_y_el_envio_no_admite_datos(
    crear_entorno: Callable[..., Any],
) -> None:
    entorno = crear_entorno(SINIESTROS_URL="http://siniestros.test")
    cambios = {"causa": "COLISION", "fechaEvento": "2026-11-17T10:00:00-05:00"}
    with respx.mock as router:
        patch = router.patch(f"http://siniestros.test{RUTA_BORRADOR}").respond(
            200, json={**BORRADOR, **cambios, "version": 2}
        )
        respuesta = entorno.pedir("PATCH", RUTA_BORRADOR, cuerpo=cambios)
        invalida = entorno.pedir(
            "POST",
            f"{RUTA_BORRADOR}/envio",
            cuerpo=cambios,
            encabezados={"Idempotency-Key": str(CLAVE)},
        )
    assert respuesta.status_code == 200 and respuesta.json()["borradorId"] == str(CLAVE)
    assert json.loads(patch.calls[0].request.content) == cambios
    assert invalida.status_code == 422


def test_consulta_enviados_usa_el_titular_resuelto_no_el_parametro(
    crear_entorno: Callable[..., Any],
) -> None:
    entorno = crear_entorno(SINIESTROS_URL="http://siniestros.test")
    with respx.mock as router:
        lista = router.get(f"http://siniestros.test/v1/clientes/{CLIENTE_ID}/siniestros").respond(
            200, json={"items": [], "siguienteCursor": None}
        )
        router.get(
            f"http://siniestros.test/v1/clientes/{CLIENTE_ID}/siniestros/{POLIZA_ID}"
        ).respond(
            404, json={"codigo": "SINIESTRO_NO_ENCONTRADO", "mensaje": "No existe", "detalles": {}}
        )
        respuesta = entorno.pedir(
            "GET", "/v1/siniestros", params={"clienteId": str(POLIZA_ID), "limite": "1"}
        )
        detalle = entorno.pedir("GET", f"/v1/siniestros/{POLIZA_ID}")
    assert respuesta.status_code == 200 and respuesta.json()["items"] == []
    assert dict(lista.calls[0].request.url.params) == {"limite": "1"}
    assert lista.calls[0].request.headers["X-Actor-Id"] == entorno.sub
    assert detalle.status_code == 404


ENLACE = {
    "url": "https://s3.test/objeto?X-Amz-Signature=secreto-efimero",
    "expiraEn": "2026-11-18T21:05:00Z",
    "nombreArchivo": "evidencia.jpg",
    "tipoContenido": "image/jpeg",
}
RUTA_ENLACE = f"/v1/siniestros/{POLIZA_ID}/evidencias/{CLAVE}/enlace"
ENLACE_INTERNO = (
    f"http://siniestros.test/v1/clientes/{CLIENTE_ID}/siniestros/"
    f"{POLIZA_ID}/evidencias/{CLAVE}/enlace"
)


def test_enlace_resuelve_titular_y_no_registra_url(crear_entorno: Callable[..., Any]) -> None:
    entorno = crear_entorno(SINIESTROS_URL="http://siniestros.test")
    with respx.mock as router:
        servicio = router.post(ENLACE_INTERNO).respond(200, json=ENLACE)
        respuesta = entorno.pedir("POST", RUTA_ENLACE, params={"clienteId": str(POLIZA_ID)})
    assert respuesta.status_code == 200 and respuesta.json() == ENLACE
    pedido = servicio.calls[0].request
    assert pedido.content == b"" and not pedido.url.params
    assert pedido.headers["X-Actor-Id"] == entorno.sub
    assert pedido.headers["X-Actor-Rol"] == "CLIENTE"
    assert "secreto-efimero" not in entorno.telemetria.texto_logs()
    assert "secreto-efimero" not in entorno.telemetria.texto_spans()


@pytest.mark.parametrize(
    ("estado", "codigo"),
    [
        (404, "SINIESTRO_NO_ENCONTRADO"),
        (404, "EVIDENCIA_NO_ENCONTRADA"),
        (404, "EVIDENCIA_NO_DISPONIBLE"),
        (409, "EVIDENCIA_NO_CARGADA"),
        (503, "DEPENDENCIA_NO_DISPONIBLE"),
    ],
)
def test_enlace_propaga_errores_sin_url(
    crear_entorno: Callable[..., Any], estado: int, codigo: str
) -> None:
    entorno = crear_entorno(SINIESTROS_URL="http://siniestros.test")
    with respx.mock as router:
        router.post(ENLACE_INTERNO).respond(
            estado, json={"codigo": codigo, "mensaje": "No disponible", "detalles": {}}
        )
        respuesta = entorno.pedir("POST", RUTA_ENLACE)
    assert respuesta.status_code == estado
    assert respuesta.json()["codigo"] == (codigo if estado != 503 else "SERVICIO_NO_DISPONIBLE")
    assert "url" not in respuesta.json() and respuesta.json()["correlationId"]


def test_enlace_sesion_invalida_o_sin_titular_no_invoca_servicio(
    crear_entorno: Callable[..., Any],
) -> None:
    entorno = crear_entorno(con_cliente=False, SINIESTROS_URL="http://siniestros.test")
    with respx.mock(assert_all_called=False) as router:
        servicio = router.post(ENLACE_INTERNO)
        invalida = entorno.pedir("POST", RUTA_ENLACE, token="token-invalido")
        sin_titular = entorno.pedir("POST", RUTA_ENLACE)
    assert invalida.status_code == 401
    assert sin_titular.status_code == 404 and not servicio.called


def test_enlace_no_devuelve_http_ni_registra_respuesta_invalida(
    crear_entorno: Callable[..., Any],
) -> None:
    entorno = crear_entorno(SINIESTROS_URL="http://siniestros.test")
    with respx.mock as router:
        router.post(ENLACE_INTERNO).respond(
            200, json={**ENLACE, "url": "http://s3.test/?secreto=abc"}
        )
        respuesta = entorno.pedir("POST", RUTA_ENLACE)
    assert respuesta.status_code == 503
    assert "secreto=abc" not in entorno.telemetria.texto_logs()


@pytest.mark.parametrize(
    "permiso", ["PRECISO", "APROXIMADO", "DENEGADO", "DENEGADO_PERMANENTEMENTE"]
)
def test_patch_ubicacion_conserva_contrato_sin_registrar_coordenadas(
    crear_entorno: Callable[..., Any],
    permiso: str,
) -> None:
    entorno = crear_entorno(SINIESTROS_URL="http://siniestros.test")
    ubicacion: dict[str, Any] = {"permiso": permiso}
    if permiso in {"PRECISO", "APROXIMADO"}:
        ubicacion.update(
            {
                "lat": 4.60971,
                "lon": -74.08175,
                "precisionM": 100.01,
                "capturadaEn": "2026-11-18T10:00:00-05:00",
            }
        )
    estado = "NO_CONFIABLE" if "lat" in ubicacion else "NO_DISPONIBLE"
    with respx.mock as router:
        servicio = router.patch(f"http://siniestros.test{RUTA_BORRADOR}").respond(
            200,
            json={**BORRADOR, "ubicacion": {**ubicacion, "estado": estado}},
        )
        r = entorno.pedir("PATCH", RUTA_BORRADOR, cuerpo={"ubicacion": ubicacion})
    assert r.status_code == 200 and r.json()["ubicacion"]["estado"] == estado
    assert json.loads(servicio.calls[0].request.content) == {"ubicacion": ubicacion}
    assert servicio.calls[0].request.headers["X-Actor-Id"] == entorno.sub
    texto = entorno.telemetria.texto_logs() + entorno.telemetria.texto_spans()
    assert "4.60971" not in texto and "-74.08175" not in texto


@pytest.mark.parametrize(
    "ubicacion",
    [
        None,
        {"permiso": "DENEGADO", "lat": 4.60971, "lon": -74.08175},
        {"permiso": "PRECISO", "estado": "DISPONIBLE"},
        {"permiso": "PRECISO", "lat": 4.60971},
    ],
)
def test_patch_ubicacion_invalida_no_invoca_servicio(
    crear_entorno: Callable[..., Any],
    ubicacion: Any,
) -> None:
    entorno = crear_entorno(SINIESTROS_URL="http://siniestros.test")
    with respx.mock(assert_all_called=False) as router:
        servicio = router.patch(f"http://siniestros.test{RUTA_BORRADOR}")
        r = entorno.pedir("PATCH", RUTA_BORRADOR, cuerpo={"ubicacion": ubicacion})
    assert r.status_code == 422 and not servicio.called
    assert "4.60971" not in r.text and "-74.08175" not in r.text
    texto = entorno.telemetria.texto_logs() + entorno.telemetria.texto_spans()
    assert "4.60971" not in texto and "-74.08175" not in texto


def test_limites_publica_regla_de_ubicacion_del_servicio(crear_entorno: Callable[..., Any]) -> None:
    entorno = crear_entorno(SINIESTROS_URL="http://siniestros.test")
    limites = {
        "maxEvidencias": 10,
        "formatosFoto": ["image/jpeg"],
        "formatosVideo": ["video/mp4"],
        "tamanoMaximoFotoBytes": 1000,
        "tamanoMaximoVideoBytes": 10000,
        "duracionMaximaVideoSegundos": 60,
        "urlCargaExpiraSegundos": 300,
        "ubicacionObligatoria": True,
        "ubicacionTiposOpcionales": ["COLISION"],
        "ubicacionPrecisionMaxM": 50,
    }
    with respx.mock as router:
        router.get("http://siniestros.test/v1/siniestros/limites").respond(200, json=limites)
        r = entorno.pedir("GET", "/v1/siniestros/limites")
    assert r.status_code == 200 and r.json() == limites
