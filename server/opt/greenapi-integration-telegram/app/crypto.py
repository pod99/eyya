import base64
import hashlib
import os
from cryptography.fernet import Fernet
from app.config import settings


def _get_key() -> bytes:
    if settings.encryption_key:
        return base64.urlsafe_b64encode(hashlib.sha256(settings.encryption_key.encode()).digest())
    key_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", ".encryption_key")
    os.makedirs(os.path.dirname(key_file), exist_ok=True)
    if os.path.exists(key_file):
        with open(key_file, "rb") as f:
            return f.read().strip()
    key = Fernet.generate_key()
    with open(key_file, "wb") as f:
        f.write(key)
    return key


_fernet = Fernet(_get_key())


def encrypt_token(token: str) -> str:
    return _fernet.encrypt(token.encode()).decode()


def decrypt_token(encrypted: str) -> str:
    return _fernet.decrypt(encrypted.encode()).decode()


def decrypt_if_encrypted(value: str | None) -> str:
    """Return decrypted Fernet value, or the original value for legacy plaintext rows."""
    if not value:
        return ""
    try:
        return decrypt_token(value)
    except Exception:
        return value


def encrypt_if_plaintext(value: str | None) -> str | None:
    """Encrypt values unless they already decrypt with the current key."""
    if value is None:
        return None
    try:
        decrypt_token(value)
        return value
    except Exception:
        return encrypt_token(value)
