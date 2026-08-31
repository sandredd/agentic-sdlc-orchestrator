"""Random short-code generation.

A random token, not an encoding of the row id: encoding the id was
considered (and rejected) specifically because it makes every code
enumerable -- given one code, incrementing it walks every other link the
service has ever created, with no auth in front of any of it. `secrets`
rather than `random`: a short code is effectively a bearer credential for
whatever it points to, not merely a display value.
"""

import secrets

_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def random_code(length: int = 6) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))
