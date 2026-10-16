"""Schemathesis 4 contra la app con repositorio en memoria y adaptador-identidad simulado."""

from dataclasses import replace
from uuid import UUID, uuid4

import httpx
import schemathesis
from clientes.config import Settings
from clientes.domain.identidad.modelos import Cliente
from clientes.main import crear_app
from fastapi.testclient import TestClient
from hypothesis import settings
from schemathesis.specs.openapi.checks import positive_data_acceptance


class _RepositorioMemoria:
    def __init__(self) -> None:
        self.clientes: dict[UUID, Cliente] = {}

    def obtener(self, cliente_id: UUID) -> Cliente | None:
        return self.clientes.get(cliente_id)

    def por_sub(self, sub: str) -> Cliente | None:
        return next((c for c in self.clientes.values() if c.sub == sub), None)

    def por_registro(self, registro_id: UUID) -> Cliente | None:
        return next((c for c in self.clientes.values() if c.registro_id == registro_id), None)

    def asegurar(self, sub: str) -> Cliente:
        existente = self.por_sub(sub)
        if existente is None:
            existente = Cliente(id=uuid4(), sub=sub)
            self.clientes[existente.id] = existente
        return existente

    def asegurar_registro(self, registro_id: UUID, correo: str) -> Cliente:
        existente = self.por_registro(registro_id)
        if existente is None:
            existente = Cliente(id=uuid4(), registro_id=registro_id, correo=correo)
            self.clientes[existente.id] = existente
        return existente

    def vincular(self, cliente_id: UUID, sub: str) -> Cliente:
        cliente = replace(self.clientes[cliente_id], sub=sub)
        self.clientes[cliente_id] = cliente
        return cliente

    def guardar(self, cliente: Cliente) -> None:
        self.clientes[cliente.id] = cliente


def _nominal(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"resultado": "APROBADO", "motivo": None})


app = crear_app(
    Settings(), transport=httpx.MockTransport(_nominal), repositorio=_RepositorioMemoria()
)
# Arranca el lifespan una vez: crea el cliente HTTP y los casos de uso en app.state.
_ciclo = TestClient(app)
_ciclo.__enter__()

schema = schemathesis.openapi.from_asgi("/openapi.json", app)


# positive_data_acceptance se excluye: los 401/403 por actor ausente o con otro rol son reglas
# de encabezados que el esquema no puede expresar; test_api.py los cubre.
@schema.parametrize()
@settings(max_examples=40, deadline=None)
def test_api_cumple_su_contrato(case: schemathesis.Case) -> None:
    case.call_and_validate(excluded_checks=[positive_data_acceptance])
