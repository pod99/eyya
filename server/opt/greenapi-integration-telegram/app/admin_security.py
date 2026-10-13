import base64
import ipaddress
import json
import logging
import secrets
import time
from datetime import datetime, timezone

from fastapi import Request
from fastapi.responses import Response, JSONResponse

from app.config import settings

logger = logging.getLogger(__name__)

PROTECTED_EXACT = {
    "/admin",
}
PROTECTED_PREFIXES = (
    "/api/admin",
    "/api/telegram",
    "/api/outbox",
    "/api/open-lines",
    "/api/channels",
    "/api/messages",
    "/api/tenant",
    "/api/activate-line",
)
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_AUTH_FAILURES: dict[str, list[float]] = {}
AUTH_FAIL_WINDOW_SECONDS = 300
AUTH_FAIL_LIMIT = 12
AUTH_FAIL_MAX_KEYS = 1000


def admin_auth_enabled() -> bool:
    return bool(settings.admin_username and settings.admin_password)


def is_protected_path(path: str) -> bool:
    if path in PROTECTED_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in PROTECTED_PREFIXES)


def _unauthorized(path: str):
    headers = {"WWW-Authenticate": 'Basic realm="SAFY Telegram Admin"'}
    if path == "/admin":
        return Response("Authentication required", status_code=401, headers=headers)
    return JSONResponse({"success": False, "message": "Authentication required"}, status_code=401, headers=headers)


def _forbidden(message: str):
    return JSONResponse({"success": False, "message": message}, status_code=403)


def _parse_basic_auth(header: str) -> tuple[str, str] | None:
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
    except Exception:
        return None
    if ":" not in raw:
        return None
    username, password = raw.split(":", 1)
    return username, password


def _auth_rate_limited(ip: str) -> bool:
    now = time.time()
    _prune_auth_failures(now)
    attempts = [
        ts for ts in _AUTH_FAILURES.get(ip or "", [])
        if now - ts <= AUTH_FAIL_WINDOW_SECONDS
    ]
    _AUTH_FAILURES[ip or ""] = attempts
    return len(attempts) >= AUTH_FAIL_LIMIT


def _record_auth_failure(ip: str):
    now = time.time()
    _prune_auth_failures(now)
    attempts = [
        ts for ts in _AUTH_FAILURES.get(ip or "", [])
        if now - ts <= AUTH_FAIL_WINDOW_SECONDS
    ]
    attempts.append(now)
    _AUTH_FAILURES[ip or ""] = attempts[-AUTH_FAIL_LIMIT:]
    if len(_AUTH_FAILURES) > AUTH_FAIL_MAX_KEYS:
        oldest = sorted(
            _AUTH_FAILURES.items(),
            key=lambda item: item[1][-1] if item[1] else 0,
        )
        for key, _ in oldest[: len(_AUTH_FAILURES) - AUTH_FAIL_MAX_KEYS]:
            _AUTH_FAILURES.pop(key, None)


def _clear_auth_failures(ip: str):
    _AUTH_FAILURES.pop(ip or "", None)


def _prune_auth_failures(now: float | None = None):
    now = now or time.time()
    stale = []
    for key, attempts in _AUTH_FAILURES.items():
        fresh = [ts for ts in attempts if now - ts <= AUTH_FAIL_WINDOW_SECONDS]
        if fresh:
            _AUTH_FAILURES[key] = fresh[-AUTH_FAIL_LIMIT:]
        else:
            stale.append(key)
    for key in stale:
        _AUTH_FAILURES.pop(key, None)


def _client_ip(request: Request) -> str:
    direct_ip = request.client.host if request.client else ""
    forwarded = request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for", "").split(",", 1)[0]
    forwarded = forwarded.strip()
    if forwarded and _trusted_proxy_ip(direct_ip):
        return forwarded
    return direct_ip


def _trusted_proxy_ip(ip: str) -> bool:
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for item in (settings.trusted_proxy_ips or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            if "/" in item:
                if addr in ipaddress.ip_network(item, strict=False):
                    return True
            elif addr == ipaddress.ip_address(item):
                return True
        except ValueError:
            logger.warning("Ignoring invalid TRUSTED_PROXY_IPS entry: %s", item)
    return False


def check_admin_auth(request: Request) -> str | None:
    if not admin_auth_enabled():
        logger.error("Admin authentication is not configured; protected request rejected")
        return None
    parsed = _parse_basic_auth(request.headers.get("authorization", ""))
    if not parsed:
        return None
    username, password = parsed
    if (
        secrets.compare_digest(username, settings.admin_username)
        and secrets.compare_digest(password, settings.admin_password)
    ):
        return username
    return None


async def write_admin_audit(actor: str, ip: str, method: str, path: str,
                            status_code: int | None = None, action: str = "",
                            details: dict | None = None):
    try:
        from app.database import async_session, AdminAuditLog, utcnow

        async with async_session() as session:
            session.add(AdminAuditLog(
                actor=actor,
                ip=ip,
                method=method,
                path=path,
                action=action or path,
                status_code=status_code,
                details_json=json.dumps(details or {}, ensure_ascii=False),
                created_at=utcnow(),
            ))
            await session.commit()
    except Exception as exc:
        logger.warning("Could not write admin audit log: %s", exc)


async def admin_auth_middleware(request: Request, call_next):
    path = request.url.path
    protected = is_protected_path(path)
    actor = None
    if protected:
        ip = _client_ip(request)
        if _auth_rate_limited(ip):
            return _forbidden("Too many authentication attempts")
        actor = check_admin_auth(request)
        if not actor:
            _record_auth_failure(ip)
            await write_admin_audit(
                actor="anonymous",
                ip=ip,
                method=request.method,
                path=path,
                status_code=401,
                action="auth_failed",
            )
            return _unauthorized(path)
        _clear_auth_failures(ip)
        if request.method in MUTATING_METHODS:
            csrf_header = request.headers.get("X-SAFY-Admin-Action", "")
            if csrf_header != "1":
                await write_admin_audit(
                    actor=actor,
                    ip=ip,
                    method=request.method,
                    path=path,
                    status_code=403,
                    action="csrf_rejected",
                )
                return _forbidden("Missing admin action header")
        request.state.admin_user = actor

    response = await call_next(request)

    if protected and (request.method in MUTATING_METHODS or path.startswith("/api/admin/actions")):
        await write_admin_audit(
            actor=actor or "admin",
            ip=_client_ip(request),
            method=request.method,
            path=path,
            status_code=response.status_code,
            action=path.rsplit("/", 1)[-1] or path,
            details={"query": dict(request.query_params)},
        )
    return response
