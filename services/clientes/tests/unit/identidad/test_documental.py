"""N1 de la HU-W30: transiciones de identidad, rechazo vs falla técnica e idempotencia."""

from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import pytest
from clientes.application.identidad.documental import CrearCliente, VerificarDocumento
from clientes.domain.identidad.modelos import (
    Cliente,
    ClienteNoEncontrado,
    ClienteSinDocumento,
    DatosCliente,
    EstadoIdentidad,
    IdentidadNoDisponible,
    ResultadoKyc,
)

pytestmark = pytest.mark.anyio

AHORA = datetime(2026, 10, 17, 12, tzinfo=UTC)
DATOS = DatosCliente("CC", "1020456789", "Natalia González Ruiz", date(1990, 4, 18))
PII = (DATOS.numero_documento, DATOS.nombre_completo, "1990-04-18")


class Veredicto:
    def __init__(self, resultado: ResultadoKyc, motivo: str | None = None) -> None:
        self.resultado = resultado
        self.motivo = motivo
        self.referencia = "kyc-ref-001"
        self.proveedor = "KYC"


class ProveedorFalso:
    def __init__(self, respuesta: Veredicto | IdentidadNoDisponible) -> None:
        self.respuesta = respuesta
        self.consultas: list[DatosCliente] = []

    async def verificar_documento(self, cliente_id: object, datos: DatosCliente) -> Veredicto:
        self.consultas.append(datos)
        if isinstance(self.respuesta, IdentidadNoDisponible):
            raise self.respuesta
        return self.respuesta


def _caso(
    repositorio: Any, respuesta: Veredicto | IdentidadNoDisponible
) -> tuple[VerificarDocumento, ProveedorFalso]:
    proveedor = ProveedorFalso(respuesta)
    return VerificarDocumento(repositorio, proveedor, lambda: AHORA), proveedor


async def _cliente_pendiente(repositorio: Any) -> Cliente:
    cliente, _ = await CrearCliente(repositorio).ejecutar(DATOS)
    return cliente


# --- Dominio: transiciones ---


def test_el_cliente_nace_pendiente_y_sin_traza() -> None:
    cliente = Cliente(id=uuid4())
    assert cliente.estado is EstadoIdentidad.PENDIENTE
    assert (cliente.verificado_en, cliente.proveedor, cliente.referencia) == (None, None, None)


def test_aprobado_verifica_y_deja_fecha_proveedor_y_referencia() -> None:
    cliente = Cliente(id=uuid4()).con_resultado(ResultadoKyc.APROBADO, AHORA, "KYC", "ref-1")
    assert cliente.verificado
    assert (cliente.verificado_en, cliente.proveedor, cliente.referencia) == (AHORA, "KYC", "ref-1")


def test_rechazado_conserva_la_traza_pero_no_la_fecha_de_verificacion() -> None:
    cliente = Cliente(id=uuid4()).con_resultado(ResultadoKyc.RECHAZADO, AHORA, "KYC", "ref-2")
    assert cliente.estado is EstadoIdentidad.RECHAZADO
    assert cliente.verificado_en is None
    assert (cliente.proveedor, cliente.referencia) == ("KYC", "ref-2")


def test_captura_invalida_no_cambia_el_cliente() -> None:
    cliente = Cliente(id=uuid4())
    assert cliente.con_resultado(ResultadoKyc.CAPTURA_INVALIDA, AHORA, "KYC", "r") is cliente


def test_datos_solo_si_el_cliente_nacio_con_documento() -> None:
    assert Cliente(id=uuid4(), sub="s").datos is None
    assert Cliente(id=uuid4(), numero_documento="1020456789").datos is None
    completo = Cliente(
        id=uuid4(),
        tipo_documento="CC",
        numero_documento="1020456789",
        nombre_completo="Natalia González Ruiz",
        fecha_nacimiento=date(1990, 4, 18),
    )
    assert completo.datos == DATOS


# --- Alta idempotente (AC4) ---


async def test_alta_crea_pendiente_y_el_mismo_documento_no_duplica(repositorio: Any) -> None:
    caso = CrearCliente(repositorio)
    primero, creado = await caso.ejecutar(DATOS)
    segundo, creado_de_nuevo = await caso.ejecutar(DATOS)
    assert (creado, creado_de_nuevo) == (True, False)
    assert primero == segundo
    assert primero.estado is EstadoIdentidad.PENDIENTE
    assert len(repositorio.clientes) == 1


async def test_alta_no_pisa_los_datos_del_cliente_existente(repositorio: Any) -> None:
    caso = CrearCliente(repositorio)
    original, _ = await caso.ejecutar(DATOS)
    otro_nombre = DatosCliente("CC", "1020456789", "Otra Persona", date(1980, 1, 1))
    existente, creado = await caso.ejecutar(otro_nombre)
    assert not creado
    assert existente.nombre_completo == original.nombre_completo


async def test_alta_no_registra_pii(repositorio: Any, telemetria: Any) -> None:
    await CrearCliente(repositorio).ejecutar(DATOS)
    assert "cliente dado de alta" in telemetria.texto_logs()
    for pii in PII:
        assert pii not in telemetria.texto_logs()
        assert pii not in telemetria.texto_spans()


# --- Verificación documental ---


async def test_aprobado_marca_verificado_con_fecha_y_referencia(repositorio: Any) -> None:
    cliente = await _cliente_pendiente(repositorio)
    caso, proveedor = _caso(repositorio, Veredicto(ResultadoKyc.APROBADO))
    resultado = await caso.ejecutar(cliente.id)
    guardado = repositorio.clientes[cliente.id]
    assert resultado.cliente == guardado
    assert guardado.verificado
    assert (guardado.verificado_en, guardado.proveedor, guardado.referencia) == (
        AHORA,
        "KYC",
        "kyc-ref-001",
    )
    assert proveedor.consultas == [DATOS]


async def test_rechazado_queda_rechazado(repositorio: Any) -> None:
    cliente = await _cliente_pendiente(repositorio)
    caso, _ = _caso(repositorio, Veredicto(ResultadoKyc.RECHAZADO, "DATOS_NO_COINCIDEN"))
    resultado = await caso.ejecutar(cliente.id)
    assert repositorio.clientes[cliente.id].estado is EstadoIdentidad.RECHAZADO
    assert resultado.resultado is ResultadoKyc.RECHAZADO


@pytest.mark.parametrize("motivo", ["TIEMPO_AGOTADO", "ERROR"])
async def test_falla_tecnica_no_es_rechazo_y_deja_pendiente(repositorio: Any, motivo: str) -> None:
    """AC3: el proveedor no responde -> sigue PENDIENTE, nunca RECHAZADO."""
    cliente = await _cliente_pendiente(repositorio)
    caso, _ = _caso(repositorio, IdentidadNoDisponible(motivo))
    with pytest.raises(IdentidadNoDisponible):
        await caso.ejecutar(cliente.id)
    assert repositorio.clientes[cliente.id].estado is EstadoIdentidad.PENDIENTE


async def test_reintento_tras_falla_tecnica_verifica_sin_crear_otro_cliente(
    repositorio: Any,
) -> None:
    cliente = await _cliente_pendiente(repositorio)
    caida, _ = _caso(repositorio, IdentidadNoDisponible("ERROR"))
    with pytest.raises(IdentidadNoDisponible):
        await caida.ejecutar(cliente.id)
    recuperada, _ = _caso(repositorio, Veredicto(ResultadoKyc.APROBADO))
    await recuperada.ejecutar(cliente.id)
    assert repositorio.clientes[cliente.id].verificado
    assert len(repositorio.clientes) == 1


async def test_confirmacion_repetida_es_idempotente(repositorio: Any) -> None:
    """AC4: un cliente ya verificado no vuelve a consultar al proveedor ni cambia."""
    cliente = await _cliente_pendiente(repositorio)
    caso, proveedor = _caso(repositorio, Veredicto(ResultadoKyc.APROBADO))
    primero = await caso.ejecutar(cliente.id)
    segundo = await caso.ejecutar(cliente.id)
    assert primero.cliente == segundo.cliente
    assert len(proveedor.consultas) == 1
    assert len(repositorio.clientes) == 1


async def test_un_rechazo_previo_se_puede_reverificar(repositorio: Any) -> None:
    cliente = await _cliente_pendiente(repositorio)
    rechazo, _ = _caso(repositorio, Veredicto(ResultadoKyc.RECHAZADO))
    await rechazo.ejecutar(cliente.id)
    aprobado, _ = _caso(repositorio, Veredicto(ResultadoKyc.APROBADO))
    await aprobado.ejecutar(cliente.id)
    assert repositorio.clientes[cliente.id].verificado


async def test_cliente_inexistente(repositorio: Any) -> None:
    caso, proveedor = _caso(repositorio, Veredicto(ResultadoKyc.APROBADO))
    with pytest.raises(ClienteNoEncontrado):
        await caso.ejecutar(uuid4())
    assert proveedor.consultas == []


async def test_cliente_sin_documento_no_se_envia_al_proveedor(repositorio: Any) -> None:
    sin_documento = repositorio.asegurar("sub-1")
    caso, proveedor = _caso(repositorio, Veredicto(ResultadoKyc.APROBADO))
    with pytest.raises(ClienteSinDocumento):
        await caso.ejecutar(sin_documento.id)
    assert proveedor.consultas == []


async def test_la_verificacion_solo_deja_metadatos_en_logs_y_spans(
    repositorio: Any, telemetria: Any
) -> None:
    cliente = await _cliente_pendiente(repositorio)
    caso, _ = _caso(repositorio, Veredicto(ResultadoKyc.APROBADO))
    await caso.ejecutar(cliente.id)
    assert "verificación documental procesada" in telemetria.texto_logs()
    assert "kyc-ref-001" in telemetria.texto_logs()
    for pii in PII:
        assert pii not in telemetria.texto_logs()
        assert pii not in telemetria.texto_spans()
