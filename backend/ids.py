import secrets
import time
import uuid


def uuid7(prefix: str) -> str:
    """Generate a sortable RFC 9562 UUIDv7 without requiring Python 3.14."""
    millis = int(time.time() * 1000) & ((1 << 48) - 1)
    value = millis << 80
    value |= 0x7 << 76
    value |= secrets.randbits(12) << 64
    value |= 0b10 << 62
    value |= secrets.randbits(62)
    return f"{prefix}_{uuid.UUID(int=value)}"
