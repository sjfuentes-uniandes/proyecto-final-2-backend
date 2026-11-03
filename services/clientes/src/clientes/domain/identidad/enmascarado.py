"""Enmascarado del número de documento: el número completo no sale de `clientes`."""


def _con_miles(digitos: str) -> str:
    grupos: list[str] = []
    while digitos:
        grupos.insert(0, digitos[-3:])
        digitos = digitos[:-3]
    return ".".join(grupos)


def enmascarar_documento(numero: str) -> str:
    """Solo los primeros y los últimos caracteres; el resto, `***`.

    Con 9 caracteres o más se ven 4 al inicio y 3 al final (1020123456 → 1.020.***.456); con menos
    se ven menos, y con menos de 4 no se ve ninguno. Un número solo de dígitos va con puntos de miles.
    """
    n = len(numero)
    if n < 4:
        return "***"
    inicio = 4 if n >= 9 else 2 if n >= 6 else 1
    fin = 3 if n >= 8 else 2 if n >= 6 else 1
    cabeza, cola = numero[:inicio], numero[-fin:]
    if numero.isdigit():
        return f"{_con_miles(cabeza)}.***.{cola}"
    return f"{cabeza}***{cola}"
