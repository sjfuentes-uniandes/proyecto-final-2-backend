"""HU-W04 AC2: pacto de un socio de referencia (consumidor) con `api-socios` (proveedor).

`SocioReferencia` es la integración mínima de un socio: cotiza, consulta y presenta la oferta con
solo lo que publica el contrato v1 (código, precio, moneda, coberturas, exclusiones y vigencia),
sin conocer el modelo interno, y ante 410 OFERTA_VENCIDA vuelve a cotizar. Lo genera esta prueba en
`contracts/pact/socio-referencia-api-socios.json`; `api-socios` lo verifica como proveedor
(test_pact_socio_referencia_proveedor.py). Los matchers son de tipo y formato: el proveedor puede
agregar campos sin romper al socio.

Las solicitudes son las que llegan a api-socios: API Gateway ya validó el token y la API key y
agregó X-Partner-Client-Id, X-Partner-Key-Id y X-Partner-Scopes (infra/platform/edge.tf).
"""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from pact import Pact, match

PACTOS = Path(__file__).parents[4] / "contracts" / "pact"
ARCHIVO = "socio-referencia-api-socios.json"
CLIENT_ID = "cliente-socio-referencia"
KEY_ID = "key-socio-referencia"
CLAVE = "00000000-0000-4000-c000-000000000010"
VIGENTE = "00000000-0000-4000-d000-000000000010"
VENCIDA = "00000000-0000-4000-d000-000000000011"
AJENA = "00000000-0000-4000-d000-000000000012"
UUID_RE = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
FECHA = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
DECIMAL = r"^-?\d+(\.\d+)?$"
SOCIO_EXISTE = "el socio SOC-000001 con permiso cotizar existe"
SOLICITUD = {
    "clienteId": "00000000-0000-4000-8000-000000000001",
    "productoId": "vida-hipotecario",
    "datosAsegurado": {
        "fechaNacimiento": "1990-04-18",
        "plazoCreditoMeses": 180,
        "sumaAsegurada": 180000000,
    },
    "coberturasOpcionales": ["ENFERMEDADES_GRAVES"],
}


# --- El socio de referencia ------------------------------------------------------------------


@dataclass(frozen=True)
class OfertaPresentable:
    """Lo que el socio muestra a su cliente."""

    cotizacion_id: str
    codigo: str
    precio: str
    moneda: str
    coberturas: list[tuple[str, str]]
    exclusiones: list[str]
    vigente_hasta: str


class DebeRecotizar(Exception):
    pass


class OfertaInexistente(Exception):
    pass


class SocioReferencia:
    def __init__(self, url: str) -> None:
        self._url = url
        self._encabezados = {
            "X-Partner-Client-Id": CLIENT_ID,
            "X-Partner-Key-Id": KEY_ID,
            "X-Partner-Scopes": "solventa/cotizar",
        }

    def cotizar(self, solicitud: dict[str, Any], clave: str) -> OfertaPresentable:
        respuesta = httpx.post(
            f"{self._url}/cotizaciones",
            json=solicitud,
            headers={**self._encabezados, "Idempotency-Key": clave},
        )
        return self._presentable(respuesta)

    def consultar(self, cotizacion_id: str) -> OfertaPresentable:
        respuesta = httpx.get(
            f"{self._url}/cotizaciones/{cotizacion_id}", headers=self._encabezados
        )
        return self._presentable(respuesta)

    @staticmethod
    def _presentable(respuesta: httpx.Response) -> OfertaPresentable:
        if respuesta.status_code == 410:
            assert respuesta.json()["detalles"]["accion"] == "NUEVA_COTIZACION"
            raise DebeRecotizar()
        if respuesta.status_code == 404:
            raise OfertaInexistente(respuesta.json()["codigo"])
        respuesta.raise_for_status()
        o = respuesta.json()
        return OfertaPresentable(
            cotizacion_id=o["cotizacionId"],
            codigo=o["codigo"],
            precio=o["precio"],
            moneda=o["moneda"],
            coberturas=[(c["nombre"], c["sumaAsegurada"]) for c in o["coberturas"]],
            exclusiones=[e["descripcion"] for e in o["exclusionesResumidas"]],
            vigente_hasta=o["vigenteHasta"],
        )


# --- Pacto -------------------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def pacto_nuevo() -> None:
    (PACTOS / ARCHIVO).unlink(missing_ok=True)


@pytest.fixture
def pacto() -> Iterator[Pact]:
    pacto = Pact("socio-referencia", "api-socios").with_specification("V4")
    yield pacto
    pacto.write_file(PACTOS)


def _oferta(cotizacion_id: str) -> dict[str, Any]:
    """Solo los campos que el socio usa, por tipo y formato."""
    return {
        "cotizacionId": match.regex(cotizacion_id, regex=UUID_RE),
        "codigo": match.regex("COT-2026-00010", regex=r"^COT-\d{4}-\d{5,}$"),
        "precio": match.regex("97460", regex=DECIMAL),
        "moneda": match.regex("COP", regex=r"^[A-Z]{3}$"),
        "coberturas": match.each_like(
            {
                "nombre": match.like("Muerte por cualquier causa"),
                "sumaAsegurada": match.regex("180000000", regex=DECIMAL),
            },
            min=1,
        ),
        "exclusionesResumidas": match.each_like(
            {"descripcion": match.like("Suicidio durante el primer año")}, min=1
        ),
        "vigenteHasta": match.regex("2026-10-29T15:00:00Z", regex=FECHA),
    }


def _encabezados(interaccion: Any) -> Any:
    return (
        interaccion.with_header("X-Partner-Client-Id", CLIENT_ID)
        .with_header("X-Partner-Key-Id", KEY_ID)
        .with_header("X-Partner-Scopes", "solventa/cotizar")
    )


def test_cotizar_y_presentar_la_oferta(pacto: Pact) -> None:
    (
        _encabezados(
            pacto.upon_receiving("una solicitud de cotización del socio")
            .given(SOCIO_EXISTE)
            .with_request("POST", "/cotizaciones")
        )
        .with_header("Idempotency-Key", match.regex(CLAVE, regex=UUID_RE))
        .with_body(SOLICITUD, content_type="application/json")
        .will_respond_with(201)
        .with_body(_oferta(VIGENTE), content_type="application/json")
    )
    with pacto.serve() as servidor:
        oferta = SocioReferencia(str(servidor.url)).cotizar(SOLICITUD, CLAVE)
    assert (oferta.codigo, oferta.precio, oferta.moneda) == ("COT-2026-00010", "97460", "COP")
    assert oferta.coberturas and oferta.exclusiones


def test_consultar_la_oferta_vigente(pacto: Pact) -> None:
    (
        _encabezados(
            pacto.upon_receiving("la consulta de una oferta vigente del socio")
            .given(SOCIO_EXISTE)
            .given(f"la cotización {VIGENTE} del socio tiene una oferta vigente")
            .with_request("GET", f"/cotizaciones/{VIGENTE}")
        )
        .will_respond_with(200)
        .with_body(_oferta(VIGENTE), content_type="application/json")
    )
    with pacto.serve() as servidor:
        oferta = SocioReferencia(str(servidor.url)).consultar(VIGENTE)
    assert oferta.cotizacion_id == VIGENTE


def test_ac3_una_oferta_vencida_pide_cotizar_de_nuevo(pacto: Pact) -> None:
    (
        _encabezados(
            pacto.upon_receiving("la consulta de una oferta vencida del socio")
            .given(SOCIO_EXISTE)
            .given(f"la oferta de la cotización {VENCIDA} del socio venció")
            .with_request("GET", f"/cotizaciones/{VENCIDA}")
        )
        .will_respond_with(410)
        .with_body(
            {"codigo": "OFERTA_VENCIDA", "detalles": {"accion": "NUEVA_COTIZACION"}},
            content_type="application/json",
        )
    )
    with pacto.serve() as servidor, pytest.raises(DebeRecotizar):
        SocioReferencia(str(servidor.url)).consultar(VENCIDA)


def test_la_oferta_de_otro_socio_no_existe(pacto: Pact) -> None:
    (
        _encabezados(
            pacto.upon_receiving("la consulta de una cotización que no es del socio")
            .given(SOCIO_EXISTE)
            .given(f"la cotización {AJENA} es de otro socio")
            .with_request("GET", f"/cotizaciones/{AJENA}")
        )
        .will_respond_with(404)
        .with_body({"codigo": "OFERTA_NO_ENCONTRADA"}, content_type="application/json")
    )
    with pacto.serve() as servidor, pytest.raises(OfertaInexistente):
        SocioReferencia(str(servidor.url)).consultar(AJENA)
