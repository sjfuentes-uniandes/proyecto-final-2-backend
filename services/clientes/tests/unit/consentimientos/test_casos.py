"""Casos de uso del consentimiento con repositorio en memoria (HU-M08: AC1, AC3, AC4)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from clientes.adapters.outbound.textos_json import TextosJson
from clientes.application.consentimientos.casos import (
    ConsultarConsentimientos,
    ConsultarTextoVigente,
    OtorgarConsentimiento,
)
from clientes.application.consentimientos.puertos import Otorgamiento
from clientes.application.identidad.casos import ConsultarCliente
from clientes.domain.consentimientos.modelos import (
    ClaveIdempotenciaEnUso,
    Consentimiento,
    ConsentimientoActivoExistente,
    ConsentimientoNoEncontrado,
    ConsentimientoYaVigente,
    EstadoConsentimiento,
    IdempotenciaConflicto,
    IdentidadNoVerificada,
    RegistroIdempotencia,
    SolicitudInvalida,
    TextoNoVigente,
    VigenciaInvalida,
)
from clientes.domain.identidad.modelos import Cliente, ClienteNoEncontrado, EstadoIdentidad

pytestmark = pytest.mark.anyio

# Las fixtures de conftest.py se tipan como Any, igual que en las pruebas de identidad.
RelojFalso = RepositorioMemoria = RepositorioConsentimientosMemoria = Any

SUB = "sub-titular"
CORRELACION = "corr-1"
TEXTOS = TextosJson.cargar()
VERSION = TEXTOS.vigente("PERFILAMIENTO_PRECIO", "es-CO").version


class RepositorioConLecturasObsoletas:
    """Simula que otra transacción ganó entre la lectura y la escritura."""

    def __init__(self, base: RepositorioConsentimientosMemoria) -> None:
        self._base = base
        self.clave_obsoleta = False
        self.activo_obsoleto = False

    async def por_clave(self, cliente_id: UUID, clave: UUID) -> RegistroIdempotencia | None:
        if self.clave_obsoleta:
            self.clave_obsoleta = False
            return None
        return await self._base.por_clave(cliente_id, clave)

    async def activo(self, cliente_id: UUID, proposito: str) -> Consentimiento | None:
        if self.activo_obsoleto:
            self.activo_obsoleto = False
            return None
        return await self._base.activo(cliente_id, proposito)

    async def crear(self, otorgamiento: Otorgamiento) -> Consentimiento:
        return await self._base.crear(otorgamiento)

    async def obtener(self, cliente_id: UUID, consentimiento_id: UUID) -> Consentimiento | None:
        return await self._base.obtener(cliente_id, consentimiento_id)

    async def listar(self, cliente_id: UUID) -> list[Consentimiento]:
        return await self._base.listar(cliente_id)


def _cliente(repositorio: RepositorioMemoria, sub: str = SUB, verificado: bool = True) -> Cliente:
    cliente = repositorio.asegurar(sub)
    if verificado:
        cliente = replace(
            cliente,
            estado=EstadoIdentidad.VERIFICADO,
            verificado_en=datetime(2026, 10, 1, tzinfo=UTC),
        )
        repositorio.guardar(cliente)
    return cliente


def _otorgar(
    repositorio: RepositorioMemoria, consentimientos: RepositorioConsentimientosMemoria
) -> OtorgarConsentimiento:
    return OtorgarConsentimiento(consentimientos, ConsultarCliente(repositorio), TEXTOS)


def _cuerpo(**cambios: Any) -> dict[str, Any]:
    return {
        "proposito": "PERFILAMIENTO_PRECIO",
        "alcances": ["FA_PRODUCTOS_VIGENTES", "DA_REGISTROS_PUBLICOS"],
        "vigencia_meses": 12,
        "version_texto": VERSION,
        "idioma": "es-CO",
        "correlation_id": CORRELACION,
    } | cambios


def _revocar(
    repo: RepositorioConsentimientosMemoria, consentimiento_id: UUID, en: datetime
) -> None:
    c = repo.consentimientos[consentimiento_id]
    repo.consentimientos[consentimiento_id] = replace(
        c, estado=EstadoConsentimiento.REVOCADO, revocado_en=en
    )


async def test_otorgar_crea_el_consentimiento_activo_y_su_evento(
    repositorio: RepositorioMemoria,
    consentimientos: RepositorioConsentimientosMemoria,
    reloj: RelojFalso,
) -> None:
    """AC1: versión del texto, fecha, propósito, alcances y vencimiento."""
    cliente = _cliente(repositorio)
    resultado = await _otorgar(repositorio, consentimientos).ejecutar(SUB, uuid4(), **_cuerpo())
    c = resultado.consentimiento
    assert not resultado.repeticion
    assert c.estado is EstadoConsentimiento.ACTIVO
    assert c.cliente_id == cliente.id
    assert c.alcances == ("DA_REGISTROS_PUBLICOS", "FA_PRODUCTOS_VIGENTES")
    assert (c.proposito, c.version_texto, c.idioma) == ("PERFILAMIENTO_PRECIO", VERSION, "es-CO")
    assert c.otorgado_en == reloj.ahora
    assert c.vigente_hasta == datetime(2027, 10, 17, 12, tzinfo=UTC)
    assert c.revocado_en is None
    assert consentimientos.eventos == [
        {"tipo": "ConsentimientoOtorgado", "agregado": c.id, "actor": SUB}
    ]


async def test_otorgar_con_seis_meses(
    repositorio: RepositorioMemoria, consentimientos: RepositorioConsentimientosMemoria
) -> None:
    _cliente(repositorio)
    resultado = await _otorgar(repositorio, consentimientos).ejecutar(
        SUB, uuid4(), **_cuerpo(vigencia_meses=6)
    )
    assert resultado.consentimiento.vigente_hasta == datetime(2027, 4, 17, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("cambio", "error"),
    [
        ({"alcances": []}, SolicitudInvalida),
        ({"vigencia_meses": None}, SolicitudInvalida),
        ({"vigencia_meses": 7}, VigenciaInvalida),
        ({"version_texto": "2000-01-01.1"}, TextoNoVigente),
    ],
)
async def test_una_solicitud_invalida_no_crea_nada(
    repositorio: RepositorioMemoria,
    consentimientos: RepositorioConsentimientosMemoria,
    cambio: dict[str, Any],
    error: type[Exception],
) -> None:
    _cliente(repositorio)
    with pytest.raises(error):
        await _otorgar(repositorio, consentimientos).ejecutar(SUB, uuid4(), **_cuerpo(**cambio))
    assert consentimientos.consentimientos == {}
    assert consentimientos.claves == {}
    assert consentimientos.eventos == []


async def test_la_validacion_va_antes_que_buscar_al_cliente(
    repositorio: RepositorioMemoria, consentimientos: RepositorioConsentimientosMemoria
) -> None:
    with pytest.raises(SolicitudInvalida):
        await _otorgar(repositorio, consentimientos).ejecutar(
            "sub-inexistente", uuid4(), **_cuerpo(alcances=None)
        )


async def test_un_titular_sin_cliente_no_otorga(
    repositorio: RepositorioMemoria, consentimientos: RepositorioConsentimientosMemoria
) -> None:
    with pytest.raises(ClienteNoEncontrado):
        await _otorgar(repositorio, consentimientos).ejecutar(SUB, uuid4(), **_cuerpo())
    assert consentimientos.eventos == []


async def test_una_identidad_no_verificada_no_otorga(
    repositorio: RepositorioMemoria, consentimientos: RepositorioConsentimientosMemoria
) -> None:
    _cliente(repositorio, verificado=False)
    with pytest.raises(IdentidadNoVerificada):
        await _otorgar(repositorio, consentimientos).ejecutar(SUB, uuid4(), **_cuerpo())
    assert consentimientos.consentimientos == {}
    assert consentimientos.eventos == []


async def test_repetir_la_misma_clave_y_cuerpo_devuelve_el_mismo_consentimiento(
    repositorio: RepositorioMemoria, consentimientos: RepositorioConsentimientosMemoria
) -> None:
    """AC4: un solo consentimiento y un solo evento."""
    _cliente(repositorio)
    caso, clave = _otorgar(repositorio, consentimientos), uuid4()
    primero = await caso.ejecutar(SUB, clave, **_cuerpo())
    # Mismo cuerpo escrito de otra forma: orden, duplicados y espacios no cuentan.
    segundo = await caso.ejecutar(
        SUB,
        clave,
        **_cuerpo(
            alcances=[" da_registros_publicos", "FA_PRODUCTOS_VIGENTES", "fa_productos_vigentes"]
        ),
    )
    assert segundo.repeticion
    assert segundo.consentimiento.id == primero.consentimiento.id
    assert len(consentimientos.consentimientos) == 1
    assert len(consentimientos.eventos) == 1


async def test_la_repeticion_antes_del_409_aunque_el_consentimiento_siga_activo(
    repositorio: RepositorioMemoria, consentimientos: RepositorioConsentimientosMemoria
) -> None:
    _cliente(repositorio)
    caso, clave = _otorgar(repositorio, consentimientos), uuid4()
    await caso.ejecutar(SUB, clave, **_cuerpo())
    assert (await caso.ejecutar(SUB, clave, **_cuerpo())).repeticion


async def test_la_repeticion_devuelve_la_representacion_actual(
    repositorio: RepositorioMemoria,
    consentimientos: RepositorioConsentimientosMemoria,
    reloj: RelojFalso,
) -> None:
    _cliente(repositorio)
    caso, clave = _otorgar(repositorio, consentimientos), uuid4()
    primero = await caso.ejecutar(SUB, clave, **_cuerpo())
    reloj.avanzar(timedelta(hours=1))
    _revocar(consentimientos, primero.consentimiento.id, reloj.ahora)
    repetido = await caso.ejecutar(SUB, clave, **_cuerpo())
    assert repetido.repeticion
    assert repetido.consentimiento.estado is EstadoConsentimiento.REVOCADO
    assert repetido.consentimiento.revocado_en == reloj.ahora


async def test_la_misma_clave_con_otro_cuerpo_es_conflicto(
    repositorio: RepositorioMemoria, consentimientos: RepositorioConsentimientosMemoria
) -> None:
    _cliente(repositorio)
    caso, clave = _otorgar(repositorio, consentimientos), uuid4()
    await caso.ejecutar(SUB, clave, **_cuerpo())
    with pytest.raises(IdempotenciaConflicto):
        await caso.ejecutar(SUB, clave, **_cuerpo(vigencia_meses=6))
    assert len(consentimientos.eventos) == 1


async def test_otra_clave_con_un_activo_vigente_es_409_con_su_id(
    repositorio: RepositorioMemoria, consentimientos: RepositorioConsentimientosMemoria
) -> None:
    _cliente(repositorio)
    caso = _otorgar(repositorio, consentimientos)
    primero = await caso.ejecutar(SUB, uuid4(), **_cuerpo())
    with pytest.raises(ConsentimientoYaVigente) as error:
        await caso.ejecutar(SUB, uuid4(), **_cuerpo(alcances=["FA_INGRESOS_AGREGADOS"]))
    assert error.value.consentimiento_id == primero.consentimiento.id
    assert len(consentimientos.eventos) == 1


async def test_la_clave_es_por_cliente(
    repositorio: RepositorioMemoria, consentimientos: RepositorioConsentimientosMemoria
) -> None:
    _cliente(repositorio, "sub-a")
    _cliente(repositorio, "sub-b")
    caso, clave = _otorgar(repositorio, consentimientos), uuid4()
    a = await caso.ejecutar("sub-a", clave, **_cuerpo())
    b = await caso.ejecutar("sub-b", clave, **_cuerpo())
    assert not b.repeticion
    assert a.consentimiento.id != b.consentimiento.id


async def test_pasadas_24_horas_la_misma_clave_se_trata_como_nueva(
    repositorio: RepositorioMemoria,
    consentimientos: RepositorioConsentimientosMemoria,
    reloj: RelojFalso,
) -> None:
    _cliente(repositorio)
    caso, clave = _otorgar(repositorio, consentimientos), uuid4()
    primero = await caso.ejecutar(SUB, clave, **_cuerpo())
    reloj.avanzar(timedelta(hours=24))
    with pytest.raises(ConsentimientoYaVigente) as error:
        await caso.ejecutar(SUB, clave, **_cuerpo())
    assert error.value.consentimiento_id == primero.consentimiento.id


@pytest.mark.parametrize("estado", [EstadoConsentimiento.REVOCADO, EstadoConsentimiento.VENCIDO])
async def test_tras_un_estado_final_se_otorga_uno_nuevo_y_el_anterior_se_conserva(
    repositorio: RepositorioMemoria,
    consentimientos: RepositorioConsentimientosMemoria,
    estado: EstadoConsentimiento,
) -> None:
    _cliente(repositorio)
    caso = _otorgar(repositorio, consentimientos)
    anterior = (await caso.ejecutar(SUB, uuid4(), **_cuerpo())).consentimiento
    consentimientos.consentimientos[anterior.id] = replace(
        anterior,
        estado=estado,
        revocado_en=anterior.otorgado_en if estado is EstadoConsentimiento.REVOCADO else None,
    )
    nuevo = (await caso.ejecutar(SUB, uuid4(), **_cuerpo())).consentimiento
    assert nuevo.id != anterior.id
    assert consentimientos.consentimientos[anterior.id].estado is estado
    assert len(consentimientos.eventos) == 2


async def test_otorgar_sobre_un_activo_vencido_lo_materializa(
    repositorio: RepositorioMemoria,
    consentimientos: RepositorioConsentimientosMemoria,
    reloj: RelojFalso,
) -> None:
    _cliente(repositorio)
    caso = _otorgar(repositorio, consentimientos)
    anterior = (await caso.ejecutar(SUB, uuid4(), **_cuerpo(vigencia_meses=6))).consentimiento
    reloj.ahora = anterior.vigente_hasta  # el límite exacto ya no está vigente
    nuevo = (await caso.ejecutar(SUB, uuid4(), **_cuerpo())).consentimiento
    assert consentimientos.consentimientos[anterior.id].estado is EstadoConsentimiento.VENCIDO
    assert nuevo.estado is EstadoConsentimiento.ACTIVO


async def test_carrera_con_la_misma_clave_se_resuelve_como_repeticion(
    repositorio: RepositorioMemoria, consentimientos: RepositorioConsentimientosMemoria
) -> None:
    repo = RepositorioConLecturasObsoletas(consentimientos)
    _cliente(repositorio)
    caso, clave = _otorgar(repositorio, repo), uuid4()
    primero = await caso.ejecutar(SUB, clave, **_cuerpo())
    repo.clave_obsoleta = repo.activo_obsoleto = True
    resultado = await caso.ejecutar(SUB, clave, **_cuerpo())
    assert resultado.repeticion
    assert resultado.consentimiento.id == primero.consentimiento.id
    assert len(consentimientos.eventos) == 1


async def test_clave_leida_antes_del_commit_y_activo_despues_es_repeticion_no_409(
    repositorio: RepositorioMemoria, consentimientos: RepositorioConsentimientosMemoria
) -> None:
    repo = RepositorioConLecturasObsoletas(consentimientos)
    _cliente(repositorio)
    caso, clave = _otorgar(repositorio, repo), uuid4()
    primero = await caso.ejecutar(SUB, clave, **_cuerpo())
    repo.clave_obsoleta = True  # la primera lectura de la clave no ve el commit; el ACTIVO sí
    resultado = await caso.ejecutar(SUB, clave, **_cuerpo())
    assert resultado.repeticion
    assert resultado.consentimiento.id == primero.consentimiento.id
    assert len(consentimientos.eventos) == 1


async def test_carrera_con_otra_clave_es_409(
    repositorio: RepositorioMemoria, consentimientos: RepositorioConsentimientosMemoria
) -> None:
    repo = RepositorioConLecturasObsoletas(consentimientos)
    _cliente(repositorio)
    caso = _otorgar(repositorio, repo)
    primero = await caso.ejecutar(SUB, uuid4(), **_cuerpo())
    repo.activo_obsoleto = True
    with pytest.raises(ConsentimientoYaVigente) as error:
        await caso.ejecutar(SUB, uuid4(), **_cuerpo())
    assert error.value.consentimiento_id == primero.consentimiento.id


async def test_carrera_con_la_misma_clave_y_otro_cuerpo_es_conflicto(
    repositorio: RepositorioMemoria,
    consentimientos: RepositorioConsentimientosMemoria,
    reloj: RelojFalso,
) -> None:
    repo = RepositorioConLecturasObsoletas(consentimientos)
    _cliente(repositorio)
    caso, clave = _otorgar(repositorio, repo), uuid4()
    primero = await caso.ejecutar(SUB, clave, **_cuerpo())
    _revocar(
        consentimientos, primero.consentimiento.id, reloj.ahora
    )  # el índice único ya no estorba
    repo.clave_obsoleta = True
    with pytest.raises(IdempotenciaConflicto):
        await caso.ejecutar(SUB, clave, **_cuerpo(vigencia_meses=6))


@pytest.mark.parametrize("error", [ConsentimientoActivoExistente, ClaveIdempotenciaEnUso])
async def test_un_conflicto_sin_causa_visible_se_propaga(
    repositorio: RepositorioMemoria,
    consentimientos: RepositorioConsentimientosMemoria,
    error: type[Exception],
) -> None:
    _cliente(repositorio)
    consentimientos.falla = error()
    with pytest.raises(error):
        await _otorgar(repositorio, consentimientos).ejecutar(SUB, uuid4(), **_cuerpo())


async def test_detalle_de_un_consentimiento_propio(
    repositorio: RepositorioMemoria,
    consentimientos: RepositorioConsentimientosMemoria,
    reloj: RelojFalso,
) -> None:
    _cliente(repositorio)
    creado = (
        await _otorgar(repositorio, consentimientos).ejecutar(SUB, uuid4(), **_cuerpo())
    ).consentimiento
    consulta = ConsultarConsentimientos(consentimientos, ConsultarCliente(repositorio))
    assert (await consulta.detalle(SUB, creado.id)).id == creado.id
    reloj.ahora = creado.vigente_hasta
    # Vencido sin materializar: el estado almacenado sigue `ACTIVO`, el derivado no.
    visto = await consulta.detalle(SUB, creado.id)
    assert visto.estado is EstadoConsentimiento.ACTIVO
    assert visto.estado_derivado is EstadoConsentimiento.VENCIDO


async def test_un_cliente_no_ve_los_consentimientos_ajenos(
    repositorio: RepositorioMemoria, consentimientos: RepositorioConsentimientosMemoria
) -> None:
    _cliente(repositorio, "sub-a")
    _cliente(repositorio, "sub-b")
    ajeno = (
        await _otorgar(repositorio, consentimientos).ejecutar("sub-a", uuid4(), **_cuerpo())
    ).consentimiento
    consulta = ConsultarConsentimientos(consentimientos, ConsultarCliente(repositorio))
    with pytest.raises(ConsentimientoNoEncontrado):
        await consulta.detalle("sub-b", ajeno.id)
    assert await consulta.listar("sub-b") == []


async def test_detalle_de_un_inexistente_o_sin_cliente(
    repositorio: RepositorioMemoria, consentimientos: RepositorioConsentimientosMemoria
) -> None:
    consulta = ConsultarConsentimientos(consentimientos, ConsultarCliente(repositorio))
    with pytest.raises(ConsentimientoNoEncontrado):
        await consulta.detalle("sub-sin-cliente", uuid4())
    _cliente(repositorio)
    with pytest.raises(ConsentimientoNoEncontrado):
        await consulta.detalle(SUB, uuid4())


async def test_listar_sin_cliente_es_una_lista_vacia(
    repositorio: RepositorioMemoria, consentimientos: RepositorioConsentimientosMemoria
) -> None:
    consulta = ConsultarConsentimientos(consentimientos, ConsultarCliente(repositorio))
    assert await consulta.listar("sub-sin-cliente") == []


async def test_listar_del_mas_reciente_al_mas_antiguo(
    repositorio: RepositorioMemoria,
    consentimientos: RepositorioConsentimientosMemoria,
    reloj: RelojFalso,
) -> None:
    _cliente(repositorio)
    caso = _otorgar(repositorio, consentimientos)
    primero = (await caso.ejecutar(SUB, uuid4(), **_cuerpo())).consentimiento
    _revocar(consentimientos, primero.id, reloj.ahora)
    reloj.avanzar(timedelta(days=1))
    segundo = (await caso.ejecutar(SUB, uuid4(), **_cuerpo())).consentimiento
    consulta = ConsultarConsentimientos(consentimientos, ConsultarCliente(repositorio))
    assert [c.id for c in await consulta.listar(SUB)] == [segundo.id, primero.id]


def test_texto_vigente_normaliza_y_devuelve_version_y_vigencias() -> None:
    texto = ConsultarTextoVigente(TEXTOS).ejecutar(" perfilamiento_precio", "en-US")
    assert texto.version == VERSION
    assert texto.idioma == "en-US"
    assert texto.vigencias_meses == (6, 12)
    assert texto.vigencia_defecto_meses == 12


@pytest.mark.parametrize(
    ("proposito", "idioma", "campos"),
    [
        (None, "es-CO", ["proposito"]),
        ("OTRO", "es-CO", ["proposito"]),
        ("PERFILAMIENTO_PRECIO", None, ["idioma"]),
        ("PERFILAMIENTO_PRECIO", "fr-FR", ["idioma"]),
        ("", "", ["proposito", "idioma"]),
    ],
)
def test_texto_vigente_con_parametros_invalidos(
    proposito: str | None, idioma: str | None, campos: list[str]
) -> None:
    with pytest.raises(SolicitudInvalida) as error:
        ConsultarTextoVigente(TEXTOS).ejecutar(proposito, idioma)
    assert list(error.value.campos) == campos
