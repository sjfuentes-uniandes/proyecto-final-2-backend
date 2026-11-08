"""HU-W24 (N1): la cola de descarte (caso de uso y API) con una DLQ simulada.

Con SQS (LocalStack) y PostgreSQL reales, en `tests/module/test_descarte_reproceso.py`.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pagos_parametricos import main
from pagos_parametricos.application.descartes import (
    ColaDescarte,
    MensajeDlq,
    Resolucion,
)
from pagos_parametricos.config import Settings
from pagos_parametricos.domain.descarte import Accion, Causa, FalloAnotado
from solventa_common.errors import ErrorNegocio

INSTANTE = datetime(2026, 9, 13, 14, 10, tzinfo=UTC)
ANALISTA = {"X-Actor-Id": "sub-analista-01", "X-Actor-Rol": "ANALISTA_SINIESTROS"}


class Reloj:
    def ahora(self) -> datetime:
        return INSTANTE


def _cuerpo(precipitacion: Any = "142,7") -> str:
    return json.dumps(
        {
            "eventId": str(uuid4()),
            "aggregateId": str(uuid4()),
            "type": "EventoParametricoObservado",
            "version": 1,
            "occurredAt": "2026-09-13T14:06:12Z",
            "correlationId": "corr-w24-01",
            "payload": {"numeroPoliza": "500-2026-000411", "precipitacionMm": precipitacion},
        }
    )


def _mensaje(cuerpo: str | None = None) -> MensajeDlq:
    return MensajeDlq(
        message_id=str(uuid4()),
        receipt_handle=f"rh-{uuid4().hex}",
        cuerpo=cuerpo or _cuerpo(),
        tipo_atributo="EventoParametricoObservado",
        recepciones=6,
        enviado_en="2026-09-13T14:06:12+00:00",
    )


@dataclass
class DlqFalsa:
    """Una DLQ con visibilidad: lo recibido queda oculto hasta liberarlo o borrarlo."""

    mensajes: list[MensajeDlq] = field(default_factory=list)
    ocultos: set[str] = field(default_factory=set)
    reenviados: list[tuple[str, str | None]] = field(default_factory=list)
    borrados: list[str] = field(default_factory=list)
    bitacora: list[str] = field(default_factory=list)
    falla_al_reenviar: BaseException | None = None

    async def recibir(self, maximo: int, visibilidad_s: int) -> list[MensajeDlq]:
        visibles = [m for m in self.mensajes if m.message_id not in self.ocultos][:maximo]
        self.ocultos |= {m.message_id for m in visibles}
        return visibles

    async def liberar(self, mensaje: MensajeDlq) -> None:
        self.ocultos.discard(mensaje.message_id)

    async def borrar(self, mensaje: MensajeDlq) -> None:
        self.bitacora.append("borrar")
        self.mensajes = [m for m in self.mensajes if m.message_id != mensaje.message_id]
        self.ocultos.discard(mensaje.message_id)
        self.borrados.append(mensaje.message_id)

    async def reenviar(self, cuerpo: str, tipo: str | None) -> None:
        self.bitacora.append("reenviar")
        if self.falla_al_reenviar is not None:
            raise self.falla_al_reenviar
        self.reenviados.append((cuerpo, tipo))

    async def retencion_dias(self) -> int:
        return 14


@dataclass
class FallosFalsos:
    anotados_: dict[str, FalloAnotado] = field(default_factory=dict)

    async def anotados(self, message_ids: Any) -> dict[str, FalloAnotado]:
        return {m: self.anotados_[m] for m in message_ids if m in self.anotados_}


@dataclass
class ResolucionesFalsas:
    guardadas: dict[str, Resolucion] = field(default_factory=dict)
    eventos: list[tuple[Resolucion, str]] = field(default_factory=list)
    bitacora: list[str] | None = None

    async def registrar(self, resolucion: Resolucion, correlation_id: str) -> Resolucion:
        if self.bitacora is not None:
            self.bitacora.append("registrar")
        if resolucion.message_id in self.guardadas:
            return self.guardadas[resolucion.message_id]
        self.guardadas[resolucion.message_id] = resolucion
        self.eventos.append((resolucion, correlation_id))
        return resolucion

    async def de(self, message_id: str) -> Resolucion | None:
        return self.guardadas.get(message_id)


@dataclass
class Escenario:
    dlq: DlqFalsa
    fallos: FallosFalsos
    resoluciones: ResolucionesFalsas
    cola: ColaDescarte


def _escenario(*mensajes: MensajeDlq, max_mensajes: int = 100) -> Escenario:
    dlq = DlqFalsa(list(mensajes))
    fallos = FallosFalsos()
    resoluciones = ResolucionesFalsas(bitacora=dlq.bitacora)
    cola = ColaDescarte(dlq, fallos, resoluciones, Reloj(), max_mensajes=max_mensajes)
    return Escenario(dlq, fallos, resoluciones, cola)


# --- Caso de uso ----------------------------------------------------------------------------------
@pytest.mark.anyio
async def test_listar_no_consume_ni_deja_ocultos() -> None:
    mensajes = [_mensaje() for _ in range(23)]
    escenario = _escenario(*mensajes)
    escenario.fallos.anotados_[mensajes[0].message_id] = FalloAnotado("envenenado", "esquema", 5)

    listado = await escenario.cola.listar()

    assert len(listado.mensajes) == 23
    assert escenario.dlq.ocultos == set()
    assert escenario.dlq.borrados == []
    assert listado.por_causa == {Causa.FORMATO_INVALIDO: 1, Causa.DESCONOCIDA: 22}
    assert (listado.retencion_dias, listado.max_intentos) == (14, 5)


@pytest.mark.anyio
async def test_listar_lee_como_mucho_el_tope() -> None:
    escenario = _escenario(*[_mensaje() for _ in range(30)], max_mensajes=12)

    assert len((await escenario.cola.listar()).mensajes) == 12


@pytest.mark.anyio
async def test_el_detalle_trae_el_mensaje_y_lo_deja_en_la_cola() -> None:
    objetivo = _mensaje()
    escenario = _escenario(_mensaje(), objetivo)

    detalle = await escenario.cola.detalle(objetivo.message_id)

    assert detalle.mensaje.message_id == objetivo.message_id
    assert detalle.verificacion is None
    assert escenario.dlq.ocultos == set()


@pytest.mark.anyio
async def test_reprocesar_registra_antes_y_reenvia_el_mismo_cuerpo_con_su_event_type() -> None:
    objetivo = _mensaje()
    escenario = _escenario(objetivo, _mensaje())

    resultados = await escenario.cola.reprocesar([objetivo.message_id], "sub-1", "corr-1")

    resolucion = resultados[objetivo.message_id]
    assert resolucion is not None and resolucion.accion is Accion.REPROCESADO
    assert escenario.dlq.reenviados == [(objetivo.cuerpo, "EventoParametricoObservado")]
    assert escenario.dlq.bitacora == ["registrar", "reenviar", "borrar"]
    assert escenario.dlq.ocultos == set()
    assert [m.message_id for m in escenario.dlq.mensajes] != [objetivo.message_id]
    ((evento, correlacion),) = escenario.resoluciones.eventos
    assert (evento.original, evento.corregido, correlacion) == (objetivo.cuerpo, None, "corr-1")


@pytest.mark.anyio
async def test_reprocesar_varios_y_los_que_no_estan_vuelven_sin_resolucion() -> None:
    a, b = _mensaje(), _mensaje()
    escenario = _escenario(a, b, _mensaje())

    resultados = await escenario.cola.reprocesar(
        [a.message_id, "no-esta", b.message_id], "sub-1", "corr-1"
    )

    assert resultados["no-esta"] is None
    assert {mid for mid, r in resultados.items() if r} == {a.message_id, b.message_id}
    assert len(escenario.dlq.mensajes) == 1


@pytest.mark.anyio
async def test_corregir_guarda_original_y_correccion_y_reenvia_la_correccion() -> None:
    objetivo = _mensaje(_cuerpo("142,7"))
    escenario = _escenario(objetivo)

    resolucion = await escenario.cola.corregir(
        objetivo.message_id, {"precipitacionMm": 142.7}, "sub-1", "corr-1", "coma decimal"
    )

    assert resolucion.accion is Accion.CORREGIDO
    assert resolucion.original == objetivo.cuerpo
    assert resolucion.corregido is not None
    corregido = json.loads(resolucion.corregido)
    assert corregido["payload"]["precipitacionMm"] == 142.7
    assert corregido["eventId"] == json.loads(objetivo.cuerpo)["eventId"]
    assert escenario.dlq.reenviados == [(resolucion.corregido, "EventoParametricoObservado")]


@pytest.mark.anyio
async def test_una_correccion_invalida_no_registra_y_deja_el_mensaje() -> None:
    objetivo = _mensaje()
    escenario = _escenario(objetivo)

    with pytest.raises(ErrorNegocio) as error:
        await escenario.cola.corregir(objetivo.message_id, {"montoPago": 1}, "sub-1", "corr-1")

    assert (error.value.codigo, error.value.estado_http) == ("CORRECCION_INVALIDA", 422)
    assert error.value.detalles == {"campos": ["montoPago"]}
    assert escenario.resoluciones.guardadas == {}
    assert escenario.dlq.ocultos == set()
    assert len(escenario.dlq.mensajes) == 1


@pytest.mark.anyio
async def test_descartar_registra_y_borra_sin_reenviar() -> None:
    objetivo = _mensaje()
    escenario = _escenario(objetivo)

    resolucion = await escenario.cola.descartar(objetivo.message_id, "sub-1", "corr-1", "duplicado")

    assert (resolucion.accion, resolucion.motivo) == (Accion.DESCARTADO, "duplicado")
    assert escenario.dlq.reenviados == []
    assert escenario.dlq.bitacora == ["registrar", "borrar"]


@pytest.mark.anyio
async def test_si_el_reenvio_falla_el_mensaje_sigue_en_la_dlq_y_el_reintento_lo_completa() -> None:
    objetivo = _mensaje()
    escenario = _escenario(objetivo)
    escenario.dlq.falla_al_reenviar = ConnectionError("sqs")

    with pytest.raises(ConnectionError):
        await escenario.cola.reprocesar([objetivo.message_id], "sub-1", "corr-1")
    assert escenario.dlq.ocultos == set() and len(escenario.dlq.mensajes) == 1

    escenario.dlq.falla_al_reenviar = None
    await escenario.cola.reprocesar([objetivo.message_id], "sub-2", "corr-2")

    assert escenario.dlq.mensajes == []
    # Una sola resolución (y un solo evento de auditoría), la del primer intento.
    assert len(escenario.resoluciones.eventos) == 1
    assert escenario.resoluciones.guardadas[objetivo.message_id].actor_id == "sub-1"


@pytest.mark.anyio
async def test_repetir_la_misma_accion_devuelve_la_resolucion_y_otra_es_409() -> None:
    objetivo = _mensaje()
    escenario = _escenario(objetivo)
    primera = await escenario.cola.descartar(objetivo.message_id, "sub-1", "corr-1", "x")

    assert await escenario.cola.descartar(objetivo.message_id, "sub-1", "corr-1", "x") == primera
    with pytest.raises(ErrorNegocio) as error:
        await escenario.cola.corregir(objetivo.message_id, {"precipitacionMm": 1}, "sub-1", "c")
    assert (error.value.codigo, error.value.estado_http) == ("DESCARTE_YA_RESUELTO", 409)


@pytest.mark.anyio
async def test_una_resolucion_previa_distinta_con_el_mensaje_aun_en_la_dlq_es_409() -> None:
    objetivo = _mensaje()
    escenario = _escenario(objetivo)
    escenario.resoluciones.guardadas[objetivo.message_id] = Resolucion(
        objetivo.message_id,
        None,
        Accion.DESCARTADO,
        Causa.DESCONOCIDA,
        "sub-0",
        None,
        objetivo.cuerpo,
        None,
        INSTANTE,
    )

    with pytest.raises(ErrorNegocio) as error:
        await escenario.cola.reprocesar([objetivo.message_id], "sub-1", "corr-1")

    assert error.value.estado_http == 409
    assert escenario.dlq.reenviados == [] and len(escenario.dlq.mensajes) == 1
    assert escenario.dlq.ocultos == set()


@pytest.mark.anyio
async def test_un_mensaje_tomado_por_otro_analista_es_404() -> None:
    objetivo = _mensaje()
    escenario = _escenario(objetivo)
    escenario.dlq.ocultos.add(objetivo.message_id)

    with pytest.raises(ErrorNegocio) as error:
        await escenario.cola.descartar(objetivo.message_id, "sub-1", "corr-1")

    assert (error.value.codigo, error.value.estado_http) == ("DESCARTE_NO_ENCONTRADO", 404)


# --- API --------------------------------------------------------------------------------------------
class PoolFalso:
    pass


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Any:
    dlq = DlqFalsa()
    fallos, resoluciones = FallosFalsos(), ResolucionesFalsas()

    def armar(pool: Any, dlq_: Any, reloj: Any, settings: Settings) -> ColaDescarte:
        return ColaDescarte(dlq_, fallos, resoluciones, Reloj())

    monkeypatch.setattr(main, "armar_cola", armar)
    app = main.crear_app(Settings(), pool=PoolFalso(), dlq=dlq)  # type: ignore[arg-type]
    with TestClient(app) as cliente:
        yield cliente, dlq, resoluciones


def test_sin_actor_es_401_y_con_otro_rol_403(api: Any) -> None:
    cliente, _, _ = api

    assert cliente.get("/v1/parametricos/descartes").status_code == 401
    otro = {"X-Actor-Id": "sub-1", "X-Actor-Rol": "ASESOR"}
    respuesta = cliente.get("/v1/parametricos/descartes", headers=otro)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (403, "ROL_NO_AUTORIZADO")


def test_lista_con_resumen_retencion_e_intentos(api: Any) -> None:
    cliente, dlq, _ = api
    dlq.mensajes = [_mensaje(), _mensaje()]

    cuerpo = cliente.get("/v1/parametricos/descartes", headers=ANALISTA).json()

    assert cuerpo["total"] == 2
    assert cuerpo["porCausa"] == [{"causa": "DESCONOCIDA", "cantidad": 2}]
    assert (cuerpo["retencionDias"], cuerpo["maxIntentos"]) == (14, 5)
    assert cuerpo["mensajes"][0]["poliza"] == "500-2026-000411"
    assert "cuerpo" not in cuerpo["mensajes"][0]


def test_detalle_con_contenido_y_clave_de_idempotencia(api: Any) -> None:
    cliente, dlq, _ = api
    mensaje = _mensaje()
    dlq.mensajes = [mensaje]

    cuerpo = cliente.get(
        f"/v1/parametricos/descartes/{mensaje.message_id}", headers=ANALISTA
    ).json()

    evento = json.loads(mensaje.cuerpo)
    assert cuerpo["claveIdempotencia"] == evento["eventId"]
    assert cuerpo["contenido"]["payload"]["precipitacionMm"] == "142,7"
    assert cuerpo["texto"] == mensaje.cuerpo
    assert cuerpo["verificacion"] is None and cuerpo["resolucion"] is None


def test_detalle_de_uno_que_no_esta_es_404(api: Any) -> None:
    cliente, _, _ = api

    respuesta = cliente.get(f"/v1/parametricos/descartes/{uuid4()}", headers=ANALISTA)

    assert (respuesta.status_code, respuesta.json()["codigo"]) == (404, "DESCARTE_NO_ENCONTRADO")


def test_reprocesar_seleccionados(api: Any) -> None:
    cliente, dlq, _ = api
    a, b = _mensaje(), _mensaje()
    dlq.mensajes = [a, b]
    ausente = str(uuid4())

    respuesta = cliente.post(
        "/v1/parametricos/descartes/reproceso",
        headers=ANALISTA,
        json={"messageIds": [a.message_id, ausente, b.message_id, a.message_id]},
    )

    assert respuesta.status_code == 200
    resultados = {r["messageId"]: r["resolucion"] for r in respuesta.json()["resultados"]}
    assert set(resultados) == {a.message_id, b.message_id, ausente}
    assert resultados[ausente] is None
    assert resultados[a.message_id]["accion"] == "REPROCESADO"
    assert resultados[a.message_id]["actorId"] == "sub-analista-01"
    assert len(dlq.reenviados) == 2


def test_corregir_devuelve_la_correccion(api: Any) -> None:
    cliente, dlq, _ = api
    mensaje = _mensaje()
    dlq.mensajes = [mensaje]

    respuesta = cliente.post(
        f"/v1/parametricos/descartes/{mensaje.message_id}/correccion",
        headers=ANALISTA,
        json={"cambios": {"precipitacionMm": 142.7}, "motivo": "coma decimal"},
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["corregido"]["payload"]["precipitacionMm"] == 142.7


@pytest.mark.parametrize(
    "cuerpo",
    [{"cambios": {}}, {"cambios": {"x": {"y": 1}}}, {}],
    ids=["vacio", "objeto", "sin-cambios"],
)
def test_una_correccion_mal_formada_es_422(api: Any, cuerpo: dict[str, Any]) -> None:
    cliente, _, _ = api

    respuesta = cliente.post(
        f"/v1/parametricos/descartes/{uuid4()}/correccion", headers=ANALISTA, json=cuerpo
    )

    assert respuesta.status_code == 422


def test_descartar_exige_motivo_y_borra(api: Any) -> None:
    cliente, dlq, resoluciones = api
    mensaje = _mensaje()
    dlq.mensajes = [mensaje]
    ruta = f"/v1/parametricos/descartes/{mensaje.message_id}/descarte"

    assert cliente.post(ruta, headers=ANALISTA, json={}).status_code == 422
    respuesta = cliente.post(ruta, headers=ANALISTA, json={"motivo": "evento de prueba"})

    assert (respuesta.status_code, respuesta.json()["accion"]) == (200, "DESCARTADO")
    assert dlq.mensajes == []
    assert resoluciones.guardadas[mensaje.message_id].motivo == "evento de prueba"


def test_sin_cola_configurada_es_503() -> None:
    with TestClient(main.crear_app(Settings())) as cliente:
        respuesta = cliente.get("/v1/parametricos/descartes", headers=ANALISTA)

    assert (respuesta.status_code, respuesta.json()["codigo"]) == (503, "COLA_NO_DISPONIBLE")
