"""EnviadorPush en memoria (HU-M11, Fase 1): sustituye al proveedor hasta el adaptador HTTP de la
Fase 2. Registra lo enviado y permite simular el rechazo y los fallos transitorios por token."""

from avisos.application.entregas.puertos import FalloTransitorio
from avisos.domain.avisos.modelos import Aviso


class PushEnMemoria:
    def __init__(self) -> None:
        self.enviados: list[tuple[str, Aviso]] = []
        self.intentos: list[str] = []
        self.rechazados: set[str] = set()
        # Fallos transitorios pendientes por token: cada intento consume uno.
        self.fallos: dict[str, list[str]] = {}

    async def enviar(self, token: str, aviso: Aviso) -> bool:
        self.intentos.append(token)
        if self.fallos.get(token):
            raise FalloTransitorio(self.fallos[token].pop(0))
        if token in self.rechazados:
            return False
        self.enviados.append((token, aviso))
        return True
