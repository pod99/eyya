import base64
import hashlib
import hmac
import os
import time

from app.config import settings


MEDIA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "media")


def _secret() -> bytes:
    secret = settings.media_url_secret or settings.encryption_key or settings.bitrix24_client_secret
    return hashlib.sha256((secret or "local-media-secret").encode()).digest()


def _signature(filename: str, expires: int) -> str:
    payload = f"{filename}:{expires}".encode()
    digest = hmac.new(_secret(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def signed_media_url(filename: str) -> str:
    expires = int(time.time()) + max(60, int(settings.media_url_ttl_seconds or 86400))
    token = _signature(filename, expires)
    return f"{settings.app_url}/media/{filename}?expires={expires}&token={token}"


def verify_media_token(filename: str, expires: int | str | None, token: str | None) -> bool:
    if not filename or not expires or not token:
        return False
    try:
        expires_int = int(expires)
    except (TypeError, ValueError):
        return False
    if expires_int < int(time.time()):
        return False
    return hmac.compare_digest(_signature(filename, expires_int), token)


def safe_media_path(filename: str) -> str | None:
    basename = os.path.basename(filename or "")
    if not basename or basename != filename:
        return None
    path = os.path.abspath(os.path.join(MEDIA_DIR, basename))
    media_root = os.path.abspath(MEDIA_DIR)
    if not path.startswith(media_root + os.sep):
        return None
    return path
