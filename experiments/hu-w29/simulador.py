"""Simulador local del portal y de la entrada de telemetría para validar carga.js sin AWS (HU-W29).

R1 y R3 responden 201 con un id (la tercera cotización, 503, para probar el reintento con la misma
clave). R5 usa el dominio real de adaptador-telemetria (verificar_firma, leer_marca,
dentro_de_desfase y validar_contrato): un evento nuevo es 202 y uno ya recibido, 200 con el mismo
eventoId. Corre desde el entorno de ese servicio:

    cd services/adaptador-telemetria && uv run python ../../experiments/hu-w29/simulador.py
    k6 run --log-format=raw --console-output=/tmp/carga.log -e CANALES=http://127.0.0.1:18929 \
      -e TOKEN=t -e R5_SECRETO=secreto-sim -e DURACION=15s experiments/hu-w29/carga.js
"""

import itertools
import json
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from adaptador_telemetria.domain.telemetria.contrato import validar_contrato
from adaptador_telemetria.domain.telemetria.firma import (
    dentro_de_desfase,
    leer_marca,
    verificar_firma,
)

SECRETO = "secreto-sim"
contador = itertools.count(1)
recibidos: dict[str, bool] = {}
rechazos: list[str] = []


class H(BaseHTTPRequestHandler):
    def _json(self, estado, cuerpo):
        datos = json.dumps(cuerpo).encode()
        self.send_response(estado)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def do_POST(self):
        crudo = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path.endswith("/telemetria/v1/eventos"):
            marca = self.headers.get("X-Telemetria-Marca-Tiempo") or ""
            if not verificar_firma(SECRETO, marca, crudo, self.headers.get("X-Telemetria-Firma")):
                return self._json(401, {"codigo": "FIRMA_INVALIDA"})
            instante = leer_marca(marca)
            if instante is None or not dentro_de_desfase(instante, datetime.now(UTC), 300):
                return self._json(401, {"codigo": "MARCA_TIEMPO_FUERA_DE_RANGO"})
            try:
                evento = validar_contrato(crudo, self.headers["X-Telemetria-Fuente"])
            except Exception as error:  # noqa: BLE001
                return self._json(
                    422, {"codigo": "CONTRATO_INVALIDO", "tipo": type(error).__name__}
                )
            eid = str(evento.evento_id)
            if eid in recibidos:
                return self._json(200, {"eventoId": eid})
            recibidos[eid] = True
            return self._json(202, {"eventoId": eid, "recibidoEn": datetime.now(UTC).isoformat()})
        n = next(contador)
        if self.path.endswith("/cotizaciones") and n == 3:
            self.send_response(503)
            self.end_headers()
            return
        cuerpo = json.loads(crudo or b"{}")
        r = {"id": str(uuid.uuid4())}
        if self.path.endswith("/suscripciones"):
            r.update(resultado="APROBADA", cotizacionId=cuerpo["cotizacionId"])
        self._json(201, r)

    def log_message(self, *a):
        pass


ThreadingHTTPServer(("127.0.0.1", 18929), H).serve_forever()
