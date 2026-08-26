import secrets
import string

ALPHABET = string.ascii_letters + string.digits
ALLOWED_CUSTOM_CHARS = set(ALPHABET + "-_")


def generate_short_code(length: int) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def is_valid_custom_code(code: str) -> bool:
    if not (3 <= len(code) <= 32):
        return False
    return all(c in ALLOWED_CUSTOM_CHARS for c in code)
