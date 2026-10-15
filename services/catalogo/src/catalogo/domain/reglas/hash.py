"""Hash canónico de la definición: no depende del orden de las claves (04 §B.4.1)."""

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def hash_canonico(definicion: Mapping[str, Any]) -> str:
    canonico = json.dumps(definicion, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonico.encode("utf-8")).hexdigest()
