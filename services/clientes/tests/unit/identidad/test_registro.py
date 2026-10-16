"""Registro previo a la cuenta: verificación con `registro_id` y vínculo en el primer ingreso."""

import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from clientes.application.identidad.casos import (
    VerificarIdentidad,
    VerificarRegistro,
    VincularRegistro,
)
from clientes.config import SECRETO_LOCAL
from clientes.domain.identidad.modelos import (
    EstadoIdentidad,
    IdentidadNoDisponible,
    RegistroConCorreoDistinto,
    RegistroYaVinculado,
    ResultadoKyc,
    TokenRegistroInvalido,
)
from solventa_common import token_registro

pytestmark = pytest.mark.anyio

AHORA = datetime(2026, 10, 15, 15, tzinfo=UTC)
REGISTRO = UUID("00000000-0000-4000-8000-0000000000aa")
CORREO = "Persona@Ejemplo.co"
GRACIA_S = 86_400


class Veredicto:
    def __init__(self, resultado: ResultadoKyc, motivo: str | None = None) -> None:
        self.resultado = resultado
        self.motivo = motivo


class VerificacionFalsa:
    def __init__(self, *respuestas: Veredicto | Exception) -> None:
        self.respuestas = list(respuestas)
        self.llamadas = 0

    async def verificar(self, cliente_id: UUID, selfie: str, prueba: dict[str, Any]) -> Veredicto:
        self.llamadas += 1
        respuesta = self.respuestas.pop(0)
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta


def _verificar(repositorio: Any, verificacion: VerificacionFalsa) -> VerificarRegistro:
    verificador = VerificarIdentidad(repositorio, verificacion, reloj=lambda: AHORA)
    return VerificarRegistro(repositorio, verificador, SECRETO_LOCAL, reloj=lambda: AHORA)


def _vincular(repositorio: Any, ahora: datetime = AHORA) -> VincularRegistro:
    return VincularRegistro(repositorio, SECRETO_LOCAL, GRACIA_S, reloj=lambda: ahora)


async def _registrar(repositorio: Any) -> str:
    """Deja el registro verificado y devuelve su token."""
    verificacion = VerificacionFalsa(Veredicto(ResultadoKyc.APROBADO))
    registro = await _verificar(repositorio, verificacion).ejecutar(REGISTRO, CORREO, "selfie", {})
    assert registro.token is not None
    return registro.token


async def test_aprobada_crea_el_cliente_del_registro_y_emite_un_token_ligado_al_correo(
    repositorio: Any,
) -> None:
    """AC1: la verificación exitosa es lo único que habilita el registro."""
    verificacion = VerificacionFalsa(Veredicto(ResultadoKyc.APROBADO))

    registro = await _verificar(repositorio, verificacion).ejecutar(REGISTRO, CORREO, "selfie", {})

    cliente = repositorio.por_registro(REGISTRO)
    assert cliente is not None and cliente.sub is None
    assert cliente.correo == "persona@ejemplo.co"
    assert registro.verificacion.cliente.estado is EstadoIdentidad.VERIFICADO
    assert registro.expira_en == AHORA + timedelta(minutes=30)
    assert registro.token is not None
    claims = token_registro.verificar(
        SECRETO_LOCAL,
        registro.token,
        correo="PERSONA@ejemplo.co",
        registro_id=str(REGISTRO),
        ahora=int(AHORA.timestamp()),
    )
    assert claims.cliente_id == str(cliente.id)


@pytest.mark.parametrize("resultado", [ResultadoKyc.CAPTURA_INVALIDA, ResultadoKyc.RECHAZADO])
async def test_sin_verificacion_exitosa_no_hay_token(
    repositorio: Any, resultado: ResultadoKyc
) -> None:
    """AC2: una captura inválida deja pendiente al cliente y no habilita el registro."""
    verificacion = VerificacionFalsa(Veredicto(resultado, "X"))

    registro = await _verificar(repositorio, verificacion).ejecutar(REGISTRO, CORREO, "selfie", {})

    assert registro.token is None and registro.expira_en is None
    assert registro.verificacion.resultado is resultado
    if resultado is ResultadoKyc.CAPTURA_INVALIDA:
        assert registro.verificacion.cliente.estado is EstadoIdentidad.PENDIENTE


async def test_error_recuperable_reintenta_con_el_mismo_cliente(repositorio: Any) -> None:
    """AC3: tras una interrupción, reintentar no crea un segundo cliente."""
    verificacion = VerificacionFalsa(
        IdentidadNoDisponible("ERROR"), Veredicto(ResultadoKyc.APROBADO)
    )
    caso = _verificar(repositorio, verificacion)

    with pytest.raises(IdentidadNoDisponible):
        await caso.ejecutar(REGISTRO, CORREO, "selfie", {})
    (pendiente,) = repositorio.clientes.values()
    registro = await caso.ejecutar(REGISTRO, CORREO, "selfie", {})

    assert len(repositorio.clientes) == 1
    assert registro.verificacion.cliente.id == pendiente.id
    assert registro.token is not None


async def test_un_registro_ya_verificado_reemite_el_token_sin_volver_al_proveedor(
    repositorio: Any,
) -> None:
    verificacion = VerificacionFalsa(Veredicto(ResultadoKyc.APROBADO))
    caso = _verificar(repositorio, verificacion)

    primero = await caso.ejecutar(REGISTRO, CORREO, "selfie", {})
    segundo = await caso.ejecutar(REGISTRO, " persona@EJEMPLO.co ", "otra", {})

    assert verificacion.llamadas == 1
    assert segundo.token is not None
    assert segundo.verificacion.cliente == primero.verificacion.cliente


async def test_el_registro_no_se_puede_reusar_con_otro_correo(repositorio: Any) -> None:
    verificacion = VerificacionFalsa(Veredicto(ResultadoKyc.APROBADO))
    caso = _verificar(repositorio, verificacion)
    await caso.ejecutar(REGISTRO, CORREO, "selfie", {})

    with pytest.raises(RegistroConCorreoDistinto):
        await caso.ejecutar(REGISTRO, "otra@ejemplo.co", "selfie", {})
    assert verificacion.llamadas == 1


async def test_un_registro_con_cuenta_no_se_vuelve_a_verificar(repositorio: Any) -> None:
    token = await _registrar(repositorio)
    await _vincular(repositorio).ejecutar(REGISTRO, "sub-1", token)
    verificacion = VerificacionFalsa()

    with pytest.raises(RegistroYaVinculado):
        await _verificar(repositorio, verificacion).ejecutar(REGISTRO, CORREO, "selfie", {})
    assert verificacion.llamadas == 0


async def test_la_evidencia_y_el_correo_no_quedan_en_logs_ni_spans(
    telemetria: Any, repositorio: Any
) -> None:
    """AC4 (back): la evidencia biométrica nunca se registra."""
    verificacion = VerificacionFalsa(Veredicto(ResultadoKyc.APROBADO))

    registro = await _verificar(repositorio, verificacion).ejecutar(
        REGISTRO, "centinela-734519@ejemplo.co", "selfie-centinela-734519", {"g": "734519"}
    )
    token = registro.token
    await _vincular(repositorio).ejecutar(REGISTRO, "sub-centinela-734519", token or "")

    assert "procesada" in telemetria.texto_logs()
    for texto in (telemetria.texto_logs(), telemetria.texto_spans()):
        assert "734519" not in texto
        assert token not in texto


async def test_vincula_el_sub_al_cliente_verificado(repositorio: Any) -> None:
    token = await _registrar(repositorio)

    cliente = await _vincular(repositorio).ejecutar(REGISTRO, "sub-1", token)

    assert cliente.sub == "sub-1" and cliente.verificado
    assert repositorio.por_sub("sub-1") == cliente


async def test_vincular_es_idempotente_para_el_mismo_sub(repositorio: Any) -> None:
    token = await _registrar(repositorio)
    caso = _vincular(repositorio)

    primero = await caso.ejecutar(REGISTRO, "sub-1", token)
    segundo = await caso.ejecutar(REGISTRO, "sub-1", token)

    assert segundo == primero
    assert len(repositorio.clientes) == 1


async def test_otro_sub_no_puede_apropiarse_de_un_registro_vinculado(repositorio: Any) -> None:
    token = await _registrar(repositorio)
    await _vincular(repositorio).ejecutar(REGISTRO, "sub-1", token)

    with pytest.raises(RegistroYaVinculado):
        await _vincular(repositorio).ejecutar(REGISTRO, "sub-2", token)


async def test_un_sub_con_cliente_propio_no_se_vincula_a_otro(repositorio: Any) -> None:
    token = await _registrar(repositorio)
    repositorio.asegurar("sub-con-cliente")

    with pytest.raises(RegistroYaVinculado):
        await _vincular(repositorio).ejecutar(REGISTRO, "sub-con-cliente", token)


@pytest.mark.parametrize(
    ("alterar", "motivo"),
    [
        (lambda t: t[:-3] + "AAA", "FIRMA"),
        (lambda t: "basura", "FORMATO"),
    ],
)
async def test_token_alterado_o_mal_formado(repositorio: Any, alterar: Any, motivo: str) -> None:
    token = await _registrar(repositorio)

    with pytest.raises(TokenRegistroInvalido) as error:
        await _vincular(repositorio).ejecutar(REGISTRO, "sub-1", alterar(token))

    assert error.value.motivo == motivo
    assert repositorio.por_registro(REGISTRO).sub is None


async def test_token_de_otro_registro(repositorio: Any) -> None:
    token = await _registrar(repositorio)

    with pytest.raises(TokenRegistroInvalido) as error:
        await _vincular(repositorio).ejecutar(uuid4(), "sub-1", token)

    assert error.value.motivo == "REGISTRO"


async def test_la_ventana_de_gracia_del_vinculo(repositorio: Any) -> None:
    """El correo puede tardar más de 30 min en confirmarse: el vínculo admite hasta 24 h más."""
    token = await _registrar(repositorio)
    dentro = AHORA + timedelta(minutes=30, hours=23)
    fuera = AHORA + timedelta(minutes=30, hours=24, seconds=1)

    with pytest.raises(TokenRegistroInvalido) as error:
        await _vincular(repositorio, fuera).ejecutar(REGISTRO, "sub-1", token)
    assert error.value.motivo == "VENCIDO"
    assert (await _vincular(repositorio, dentro).ejecutar(REGISTRO, "sub-1", token)).sub == "sub-1"


async def test_token_firmado_de_un_registro_que_no_existe(repositorio: Any) -> None:
    token, _ = token_registro.emitir(
        SECRETO_LOCAL, str(REGISTRO), str(uuid4()), CORREO, ahora=int(AHORA.timestamp())
    )

    with pytest.raises(TokenRegistroInvalido) as error:
        await _vincular(repositorio).ejecutar(REGISTRO, "sub-1", token)

    assert error.value.motivo == "CLIENTE"


async def test_token_con_otro_cliente_del_mismo_registro(repositorio: Any) -> None:
    await _registrar(repositorio)
    token, _ = token_registro.emitir(
        SECRETO_LOCAL, str(REGISTRO), str(uuid4()), CORREO, ahora=int(AHORA.timestamp())
    )

    with pytest.raises(TokenRegistroInvalido) as error:
        await _vincular(repositorio).ejecutar(REGISTRO, "sub-1", token)

    assert error.value.motivo == "CLIENTE"


async def test_un_cliente_no_verificado_no_se_vincula(repositorio: Any) -> None:
    cliente = repositorio.asegurar_registro(REGISTRO, "persona@ejemplo.co")
    token, _ = token_registro.emitir(
        SECRETO_LOCAL, str(REGISTRO), str(cliente.id), CORREO, ahora=int(AHORA.timestamp())
    )

    with pytest.raises(TokenRegistroInvalido) as error:
        await _vincular(repositorio).ejecutar(REGISTRO, "sub-1", token)

    assert error.value.motivo == "NO_VERIFICADO"


@pytest.mark.parametrize(
    ("falla", "resultado"),
    [(None, "ok"), ("token", "rechazado"), ("vinculado", "rechazado")],
)
async def test_metrica_de_la_vinculacion(
    telemetria: Any, repositorio: Any, falla: str | None, resultado: str
) -> None:
    token = await _registrar(repositorio)
    if falla == "vinculado":
        await _vincular(repositorio).ejecutar(REGISTRO, "sub-1", token)
    with contextlib.suppress(TokenRegistroInvalido, RegistroYaVinculado):
        await _vincular(repositorio).ejecutar(
            REGISTRO,
            "sub-2" if falla == "vinculado" else "sub-1",
            "basura" if falla == "token" else token,
        )

    (span,) = [s for s in telemetria.lista_spans() if s.name == "registro.vincular"][-1:]
    assert span.attributes["result"] == resultado
