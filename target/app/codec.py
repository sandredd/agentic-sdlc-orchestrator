"""Base62 short-code generation.

A monotonic row id is encoded rather than drawing a random token: it makes
every code unique by construction, so the hot create path never needs a
collision-retry loop. Codes are offset by a fixed power of the base so early
ids ("1", "2", ...) still decode to a minimum length instead of looking
suspiciously short next to later ones -- the same trick as zero-padding in
base 10, where offsetting by 10**(n-1) is what keeps a 6-digit field from
ever showing fewer than 6 digits.
"""

_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_BASE = len(_ALPHABET)


def encode(n: int, min_length: int = 6) -> str:
    if n < 0:
        raise ValueError("cannot encode a negative id")
    offset = n + _BASE ** (min_length - 1)
    digits = []
    while offset:
        offset, rem = divmod(offset, _BASE)
        digits.append(_ALPHABET[rem])
    return "".join(reversed(digits))


def decode(code: str, min_length: int = 6) -> int:
    n = 0
    for ch in code:
        n = n * _BASE + _ALPHABET.index(ch)
    return n - _BASE ** (min_length - 1)
