"""N2 HU-W03: cálculo de la prima con catalogo, clientes y PostgreSQL efímeros (arnés de
conftest.py) y Valkey para el Cache-Aside.

- AC1: la prima se calcula en la moneda del producto y la cotización persistida conserva la
  versión y el hash de las reglas que publicó catalogo, las entradas mínimas y el desglose.
- AC2: producto inexistente, inactivo, vencido o sin reglas vigentes → causa funcional estable,
  sin filas.
- AC3: mismo producto, datos y versión → misma prima, mismo desglose y misma huella.
- Seguridad: un socio no ve cotizaciones de otro; los logs y spans de la suite se revisan al final
  (test_zz_centinelas.py).
"""

import time
from collections.abc import Callable, Iterator
from typing import Any
from uuid import uuid4

import psycopg
import pytest

pytest.importorskip("docker")

ASESOR = {"X-Actor-Id": "asesor-sintetico", "X-Actor-Rol": "ASESOR"}
SOCIO_1 = {"X-Actor-Id": "SOC-000001", "X-Actor-Rol": "SOCIO"}
SOCIO_2 = {"X-Actor-Id": "SOC-000002", "X-Actor-Rol": "SOCIO"}
C01 = "00000000-0000-4000-8000-000000000001"
DATOS_C01 = {"fechaNacimiento": "1990-04-18", "plazoCreditoMeses": 180, "sumaAsegurada": 180000000}
IMAGEN_VALKEY = "valkey/valkey:8.0.2-alpine"


def cuerpo(producto: str = "vida-hipotecario", **cambios: Any) -> dict[str, Any]:
    return {"clienteId": C01, "productoId": producto, "datosAsegurado": DATOS_C01, **cambios}


def cotizar(
    app: Any, datos: dict[str, Any], actor: dict[str, str] = ASESOR, clave: str = ""
) -> Any:
    encabezados = {**actor, "Idempotency-Key": clave or str(uuid4())}
    return app.cliente.post("/v1/cotizaciones", json=datos, headers=encabezados)


def filas(app: Any, sql: str, *parametros: Any) -> list[tuple[Any, ...]]:
    return app.bases.filas(sql, *parametros)


def en_catalogo(app: Any, sql: str, *parametros: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(app.bases.catalogo, autocommit=True) as conexion:
        cursor = conexion.execute(sql, parametros)
        return cursor.fetchall() if cursor.description else []


def producto_extra(app: Any, producto_id: str, estado: str, reglas_desde: str | None) -> None:
    """Un producto sintético en la base de catalogo (vigente desde 2026-09-01, sin fin)."""
    en_catalogo(
        app,
        "INSERT INTO productos (id, version, nombre, ramo, pais, moneda, estado, vigente_desde, "
        "coberturas) SELECT %s, version, nombre, ramo, pais, moneda, %s, vigente_desde, "
        "coberturas FROM productos WHERE id = 'vida-hipotecario' ON CONFLICT (id) DO NOTHING",
        producto_id,
        estado,
    )
    if reglas_desde:
        en_catalogo(
            app,
            "INSERT INTO reglas_prima (producto_id, version, vigente_desde, definicion, hash, "
            "publicado_por) SELECT %s, version, %s, jsonb_set(definicion, '{productoId}', "
            "to_jsonb(%s::text)), hash, 'prueba' FROM reglas_prima "
            "WHERE producto_id = 'vida-hipotecario' ON CONFLICT DO NOTHING",
            producto_id,
            reglas_desde,
            producto_id,
        )


# --- AC1 -------------------------------------------------------------------------------------


def test_ac1_prima_persistida_con_la_version_de_reglas(cotizacion: Callable[..., Any]) -> None:
    app = cotizacion()
    respuesta = cotizar(app, cuerpo(coberturasOpcionales=["ENFERMEDADES_GRAVES"]))
    assert respuesta.status_code == 201, respuesta.text
    creada = respuesta.json()
    assert (creada["moneda"], creada["primaTotal"]) == ("COP", "97460")
    hash_publicado = en_catalogo(
        app,
        "SELECT hash FROM reglas_prima WHERE producto_id = 'vida-hipotecario' "
        "AND version = '2026.09.1'",
    )[0][0]
    ((version, hash_reglas, moneda, prima, entradas, desglose, correlacion),) = filas(
        app,
        "SELECT version_reglas, hash_reglas, moneda, prima_total, entradas, desglose, "
        "correlation_id FROM cotizaciones WHERE id = %s",
        creada["id"],
    )
    assert (version, hash_reglas, moneda) == ("2026.09.1", hash_publicado, "COP")
    assert str(prima) == "97460.0000"
    assert entradas == {
        "sumaAsegurada": "180000000",
        "plazoCreditoMeses": 180,
        "edad": 36,
        "nivelRiesgo": None,
        "coberturasOpcionales": ["ENFERMEDADES_GRAVES"],
    }
    assert desglose == creada["desglose"] and correlacion
    leida = app.cliente.get(f"/v1/cotizaciones/{creada['id']}", headers=ASESOR).json()
    assert leida["desglose"] == creada["desglose"] and leida["pasos"] == []


def test_ac1_con_el_perfil_de_riesgo(cotizacion: Callable[..., Any]) -> None:
    app = cotizacion()
    perfil = app.enviar("01")
    assert perfil.status_code == 201, perfil.text
    nivel = perfil.json()["nivelRiesgo"]
    respuesta = cotizar(app, cuerpo(perfilId=perfil.json()["id"]))
    assert respuesta.status_code == 201, respuesta.text
    assert respuesta.json()["entradas"]["nivelRiesgo"] == nivel
    assert respuesta.json()["desglose"]["factores"][0] == {
        "codigo": "NIVEL_RIESGO",
        "valor": nivel,
        "factor": {"BAJO": "0.90", "MEDIO": "1.00", "ALTO": "1.30"}[nivel],
    }
    otros_datos = cotizar(
        app,
        cuerpo(
            perfilId=perfil.json()["id"], datosAsegurado={**DATOS_C01, "plazoCreditoMeses": 240}
        ),
    )
    assert otros_datos.json()["codigo"] == "PERFIL_NO_CORRESPONDE"


# --- AC2 -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("producto", "preparar", "codigo"),
    [
        ("no-existe-w03", None, "PRODUCTO_NO_ENCONTRADO"),
        ("vida-hipotecario-2025", None, "PRODUCTO_FUERA_DE_VIGENCIA"),
        ("vida-inactivo-w03", ("INACTIVO", "2026-09-01"), "PRODUCTO_INACTIVO"),
        ("vida-sin-reglas-w03", ("ACTIVO", "2027-01-01"), "REGLAS_NO_VIGENTES"),
    ],
)
def test_ac2_causa_funcional_estable_sin_filas(
    cotizacion: Callable[..., Any], producto: str, preparar: tuple[str, str] | None, codigo: str
) -> None:
    app = cotizacion(catalogo_cache_ttl_s=0)
    if preparar:
        producto_extra(app, producto, *preparar)
    antes = filas(app, "SELECT count(*) FROM cotizaciones")[0][0]
    respuesta = cotizar(app, cuerpo(producto))
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (422, codigo)
    assert filas(app, "SELECT count(*) FROM cotizaciones")[0][0] == antes
    condiciones = app.cliente.get(f"/v1/productos/{producto}/condiciones", headers=ASESOR)
    assert condiciones.json()["codigo"] == codigo


def test_datos_fuera_de_rango_y_cliente_inexistente(cotizacion: Callable[..., Any]) -> None:
    app = cotizacion()
    fuera = cotizar(app, cuerpo(datosAsegurado={**DATOS_C01, "sumaAsegurada": 1400000000}))
    assert fuera.json() | {"correlationId": None} == {
        "codigo": "DATOS_FUERA_DE_RANGO",
        "mensaje": "Hay datos fuera de los límites del producto",
        "correlationId": None,
        "detalles": {"campos": ["datosAsegurado.sumaAsegurada"]},
    }
    sin_cliente = cotizar(app, cuerpo(clienteId="00000000-0000-4000-8000-0000000000ff"))
    assert (sin_cliente.status_code, sin_cliente.json()["codigo"]) == (404, "CLIENTE_NO_ENCONTRADO")


# --- AC3 -------------------------------------------------------------------------------------


def test_ac3_misma_entrada_y_version_mismo_resultado(cotizacion: Callable[..., Any]) -> None:
    app = cotizacion()
    primera = cotizar(app, cuerpo()).json()
    segunda = cotizar(app, cuerpo()).json()
    assert primera["id"] != segunda["id"]
    for campo in ("primaTotal", "desglose", "huellaCalculo", "versionReglas"):
        assert primera[campo] == segunda[campo]
    huellas = filas(
        app,
        "SELECT DISTINCT huella_calculo, hash_reglas FROM cotizaciones WHERE id IN (%s, %s)",
        primera["id"],
        segunda["id"],
    )
    assert len(huellas) == 1


def test_idempotencia(cotizacion: Callable[..., Any]) -> None:
    app = cotizacion()
    clave = str(uuid4())
    primera = cotizar(app, cuerpo(), clave=clave)
    repetida = cotizar(app, cuerpo(), clave=clave)
    otra = cotizar(app, cuerpo(coberturasOpcionales=["AUXILIO_FUNERARIO"]), clave=clave)
    assert (primera.status_code, repetida.status_code) == (201, 200)
    assert repetida.json()["id"] == primera.json()["id"]
    assert otra.json()["codigo"] == "IDEMPOTENCIA_CONFLICTO"


# --- Seguridad -------------------------------------------------------------------------------


def test_un_socio_no_ve_cotizaciones_de_otro(cotizacion: Callable[..., Any]) -> None:
    app = cotizacion()
    propia = cotizar(app, cuerpo(), actor=SOCIO_1).json()
    assert propia["socioId"] == "SOC-000001"
    url = f"/v1/cotizaciones/{propia['id']}"
    assert app.cliente.get(url, headers=SOCIO_1).status_code == 200
    ajena = app.cliente.get(url, headers=SOCIO_2)
    assert (ajena.status_code, ajena.json()["codigo"]) == (404, "COTIZACION_NO_ENCONTRADA")
    con_perfil = cotizar(app, cuerpo(perfilId=str(uuid4())), actor=SOCIO_1)
    assert con_perfil.json()["codigo"] == "ROL_NO_AUTORIZADO"


def test_cotizaciones_inmutables(cotizacion: Callable[..., Any]) -> None:
    app = cotizacion()
    creada = cotizar(app, cuerpo()).json()
    with (
        psycopg.connect(app.bases.cotizacion, autocommit=True) as conexion,
        pytest.raises(psycopg.errors.RaiseException, match="cotizaciones es inmutable"),
    ):
        conexion.execute("UPDATE cotizaciones SET prima_total = 0 WHERE id = %s", (creada["id"],))


def test_condiciones_del_producto(cotizacion: Callable[..., Any]) -> None:
    app = cotizacion()
    respuesta = app.cliente.get("/v1/productos/vida-hipotecario/condiciones", headers=ASESOR)
    assert respuesta.status_code == 200
    assert respuesta.json()["limites"]["sumaAsegurada"]["maximo"] == "1000000000"


# --- Cache-Aside -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def valkey() -> Iterator[str]:
    import redis
    from testcontainers.core.container import DockerContainer

    with DockerContainer(IMAGEN_VALKEY).with_exposed_ports(6379) as contenedor:
        url = f"redis://{contenedor.get_container_host_ip()}:{contenedor.get_exposed_port(6379)}"
        limite = time.monotonic() + 30
        while True:
            try:
                if redis.Redis.from_url(url).ping():
                    break
            except redis.RedisError:
                if time.monotonic() > limite:
                    raise
                time.sleep(0.2)
        yield url


def test_cotizacion_lee_la_cache_que_llena_catalogo(
    cotizacion: Callable[..., Any], servir_app: Any, valkey: str, bases: Any
) -> None:
    """catalogo (con CACHE_URL) llena Valkey; cotizacion la lee aunque catalogo ya no responda."""
    import httpx
    from catalogo.config import Settings as SettingsCatalogo
    from catalogo.main import crear_app as crear_catalogo

    # La primera operación abre la conexión: 50 ms (producción) no alcanzan en la CI y la caché
    # se omitiría en silencio. Aquí se prueba el contrato de claves, no la latencia.
    settings = SettingsCatalogo(
        SOLVENTA_DATABASE_URL=bases.catalogo, CACHE_URL=valkey, cache_timeout_ms=2000
    )
    with servir_app(crear_catalogo(settings)) as url:
        httpx.get(f"{url}/v1/productos/vida-hipotecario", timeout=10).raise_for_status()
        httpx.get(
            f"{url}/v1/productos/vida-hipotecario/reglas-prima/vigente", timeout=10
        ).raise_for_status()
    # catalogo detenido: solo queda la caché.
    app = cotizacion(CATALOGO_URL=url, CACHE_URL=valkey, cache_timeout_ms=2000)
    respuesta = cotizar(app, cuerpo())
    assert respuesta.status_code == 201, respuesta.text
    paso = next(p for p in respuesta.json()["pasos"] if p["codigo"] == "CATALOGO_Y_REGLAS")
    assert paso["origen"] == "CACHE"
    # Sin caché ni catalogo: falla cerrada.
    caida = cotizacion(CATALOGO_URL=url)
    respuesta = cotizar(caida, cuerpo())
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (503, "DEPENDENCIA_NO_DISPONIBLE")
