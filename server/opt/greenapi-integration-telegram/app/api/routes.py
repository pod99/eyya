import json
import logging
import time
import base64
import binascii
import asyncio
import hashlib
import hmac
import html as html_lib
import ipaddress
import os
import tempfile
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Request, Response, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select, update, delete, or_, and_
from app.database import async_session, Tenant, Connector, Channel, ChatLink, Message, TelegramAccount, OutboxEntry, QuickReplyTemplate, utcnow
from app.config import settings
from app.crypto import encrypt_token, decrypt_token, decrypt_if_encrypted
from app.bitrix import client as bx
from app.bitrix.crm import create_or_get_lead
from app.providers.registry import get_provider, load_providers_from_settings
from app.outbox import outbox, OutboxMessage
from app.api.telegram_routes import _telegram_inbound_callback, _save_media, _build_bitrix_file_entry
from app.logging_config import log_api_call, log_message_event
from app.media_security import signed_media_url
from app.admin_security import check_admin_auth

logger = logging.getLogger(__name__)
router = APIRouter()
_TYPING_STATUSES: dict[tuple[str, str], dict] = {}
WIDGET_TOKEN_TTL_SECONDS = 12 * 3600

import re as _re


def _js_literal(value) -> str:
    """Return a JavaScript literal safe to embed inside a <script> tag."""
    encoded = json.dumps(str(value or ""), ensure_ascii=False)
    return (
        encoded
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _max_body_bytes(widget: bool = False) -> int:
    raw = settings.max_widget_request_bytes if widget else settings.max_public_request_bytes
    return max(1024, int(raw or 0))


async def _read_limited_body(request: Request, max_bytes: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise HTTPException(status_code=413, detail="Request body too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length")
    raw = await request.body()
    if len(raw) > max_bytes:
        raise HTTPException(status_code=413, detail="Request body too large")
    return raw


def _allowed_bitrix_suffixes() -> tuple[str, ...]:
    values = [
        item.strip().lower().lstrip(".")
        for item in (settings.allowed_bitrix_domain_suffixes or "").split(",")
        if item.strip()
    ]
    return tuple(values)


def _normalize_bitrix_domain(domain: str) -> str:
    normalized = (domain or "").strip().lower().rstrip(".")
    if not normalized or "://" in normalized or "/" in normalized or "\\" in normalized:
        raise HTTPException(status_code=400, detail="Invalid Bitrix domain")
    if not _re.fullmatch(r"[a-z0-9.-]{3,255}", normalized):
        raise HTTPException(status_code=400, detail="Invalid Bitrix domain")
    try:
        ip = ipaddress.ip_address(normalized)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise HTTPException(status_code=400, detail="Invalid Bitrix domain")
        raise HTTPException(status_code=400, detail="Bitrix domain must be a hostname")
    except ValueError:
        pass
    blocked = {"localhost", "metadata.google.internal"}
    if normalized in blocked or normalized.endswith(".local"):
        raise HTTPException(status_code=400, detail="Invalid Bitrix domain")
    suffixes = _allowed_bitrix_suffixes()
    if suffixes and not any(normalized == suffix or normalized.endswith("." + suffix) for suffix in suffixes):
        raise HTTPException(status_code=400, detail="Bitrix domain is not allowed")
    return normalized


def _verify_bearer_or_hmac(request: Request, raw_bytes: bytes, secret: str) -> bool:
    secret = str(secret or "")
    if not secret:
        return False
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return hmac.compare_digest(auth.split(" ", 1)[1].strip(), secret)
    token = request.headers.get("X-SAFY-Inbound-Token") or request.query_params.get("token")
    if token and hmac.compare_digest(str(token), secret):
        return True
    signature = request.headers.get("X-SAFY-Hmac-Sha256") or request.headers.get("X-Hub-Signature-256")
    if not signature:
        return False
    provided = signature.strip()
    if provided.startswith("sha256="):
        provided = provided.split("=", 1)[1]
    expected = hmac.new(secret.encode("utf-8"), raw_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided, expected)


def _widget_payload_fingerprint(account_id: int, recipient: str, text: str, files: list,
                                create_ol: bool, create_lead: bool, add_timeline: bool,
                                entity_type: str, entity_id: str) -> str:
    file_parts = []
    for file_info in files:
        content = file_info.get("bytes") or b""
        file_parts.append({
            "name": file_info.get("name", ""),
            "content_type": file_info.get("content_type", ""),
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        })
    payload = {
        "account_id": int(account_id or 0),
        "recipient": recipient,
        "text": text,
        "files": file_parts,
        "create_ol": bool(create_ol),
        "create_lead": bool(create_lead),
        "add_timeline": bool(add_timeline),
        "entity_type": str(entity_type or ""),
        "entity_id": str(entity_id or ""),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _raw_payload_dict(raw_payload_json: str | None) -> dict:
    if not raw_payload_json:
        return {}
    try:
        parsed = json.loads(raw_payload_json)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}

def _parse_bitrix_text(text: str) -> tuple[str, str]:
    """Extract operator name and clean message from Bitrix24 BBCode.
    Returns (operator_name_or_empty, clean_text).
    Format: [b]Name:[/b] [br]actual message
    """
    m = _re.match(r'^\[b\](.*?):\[/b\]\s*(?:\[br\])?\s*(.*)', text or "", _re.DOTALL | _re.IGNORECASE)
    if m:
        operator_name = m.group(1).strip()
        clean = m.group(2)
    else:
        operator_name = ""
        clean = text or ""
    # Strip remaining BBCode tags, convert [br] to newline
    clean = _re.sub(r'\[br\]', '\n', clean, flags=_re.IGNORECASE)
    clean = _re.sub(r'\[b\](.*?)\[/b\]', r'\1', clean, flags=_re.IGNORECASE | _re.DOTALL)
    clean = _re.sub(r'\[i\](.*?)\[/i\]', r'\1', clean, flags=_re.IGNORECASE | _re.DOTALL)
    clean = _re.sub(r'\[u\](.*?)\[/u\]', r'\1', clean, flags=_re.IGNORECASE | _re.DOTALL)
    clean = _re.sub(r'\[url[^\]]*\](.*?)\[/url\]', r'\1', clean, flags=_re.IGNORECASE | _re.DOTALL)
    clean = _re.sub(r'\[[^\]]+\]', '', clean)
    return operator_name, clean.strip()


def _primary_connector_id(raw_connector_id: str = "") -> str:
    return settings.primary_connector_id or raw_connector_id or settings.connector_id


def _primary_line_id(raw_line_id=None) -> int:
    if settings.primary_bitrix_line_id:
        return settings.primary_bitrix_line_id
    return int(raw_line_id or 0)


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "y", "yes", "true", "on"}
    return default


def _data_get_any(data: dict, *keys: str):
    for key in keys:
        if key in data and data.get(key) not in (None, ""):
            return data.get(key)
    return None


def _typing_chat_id(data: dict) -> str:
    chat = data.get("chat") if isinstance(data.get("chat"), dict) else {}
    session = data.get("session") if isinstance(data.get("session"), dict) else {}
    im = data.get("im") if isinstance(data.get("im"), dict) else {}
    value = (
        _data_get_any(data, "CHAT_ID", "chat_id", "DIALOG_ID", "dialog_id") or
        _data_get_any(chat, "id", "ID", "CHAT_ID", "chat_id") or
        _data_get_any(session, "CHAT_ID", "chat_id", "DIALOG_ID", "dialog_id") or
        _data_get_any(im, "chat_id", "CHAT_ID", "dialog_id", "DIALOG_ID")
    )
    return str(value or "").strip()


def _record_typing_status(domain: str, data: dict) -> dict:
    chat_id = _typing_chat_id(data)
    raw_status = str(_data_get_any(data, "STATUS", "status", "TYPING", "typing") or "").strip().lower()
    is_typing = raw_status in {"typing", "start", "y", "yes", "1", "true", "write"}
    if not raw_status:
        is_typing = True
    if chat_id:
        _TYPING_STATUSES[(domain or "", chat_id)] = {
            "typing": is_typing,
            "chat_id": chat_id,
            "updated_at": time.time(),
            "expires_at": time.time() + (8 if is_typing else 1),
        }
    return {"success": True, "typing": is_typing, "chat_id": chat_id}


def _widget_secret() -> bytes:
    secret = settings.media_url_secret or settings.encryption_key or settings.bitrix24_client_secret
    return str(secret or "safy-widget-dev-secret").encode("utf-8")


def _make_widget_token(domain: str) -> str:
    normalized_domain = (domain or "").strip().lower()
    expires = int(time.time()) + WIDGET_TOKEN_TTL_SECONDS
    payload = f"{normalized_domain}|{expires}"
    sig = hmac.new(_widget_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    raw = f"{payload}|{sig}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _verify_widget_token(token: str, domain: str) -> bool:
    if not token or not domain:
        return False
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        token_domain, expires_raw, sig = raw.split("|", 2)
        expires = int(expires_raw)
    except Exception:
        return False
    if token_domain != (domain or "").strip().lower() or expires < int(time.time()):
        return False
    payload = f"{token_domain}|{expires}"
    expected = hmac.new(_widget_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


async def _valid_bitrix_widget_bootstrap(request: Request, domain: str, body: dict) -> bool:
    if check_admin_auth(request):
        return True
    auth_id = _bitrix_access_token_from_request(request, body)
    if not domain or not auth_id:
        return False
    return await _valid_bitrix_access_token(domain, str(auth_id))


async def _valid_bitrix_access_token(domain: str, access_token: str) -> bool:
    if not domain or not access_token:
        return False
    try:
        result = await bx._call(domain, str(access_token), "user.current", {}, timeout=10)
        return bool(result and (result.get("ID") or result.get("id")))
    except Exception as exc:
        logger.warning("Bitrix token validation failed for %s: %s", domain, exc)
        return False


def _inbound_message_identity(
    provider: str,
    channel_id: str,
    body: dict,
    external_chat_id: str,
    external_user_id: str,
    text: str,
    attachments,
) -> tuple[str, str, bool]:
    raw_id = (
        body.get("provider_message_id")
        or body.get("message_id")
        or body.get("id")
        or body.get("event_id")
        or body.get("request_id")
        or ""
    )
    provider_message_id = str(raw_id).strip()
    generated = False
    if not provider_message_id:
        stable_time = body.get("created_at") or body.get("timestamp") or body.get("date") or ""
        fingerprint_payload = {
            "provider": provider,
            "channel_id": channel_id,
            "external_chat_id": external_chat_id,
            "external_user_id": external_user_id,
            "text": text,
            "attachments": attachments or [],
            "stable_time": stable_time,
        }
        if not stable_time:
            fingerprint_payload["received_at_ns"] = time.time_ns()
        digest = hashlib.sha256(
            json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:24]
        provider_message_id = f"generated_{digest}"
        generated = True
    idempotency_key = f"{provider}_{channel_id}_{provider_message_id}_inbound"
    return provider_message_id, idempotency_key, generated


def _widget_domain_from_request(request: Request, body: dict | None = None) -> str:
    if body is None:
        body = {}
    return (
        str(body.get("domain") or body.get("DOMAIN") or "").strip()
        or str(request.query_params.get("domain") or request.query_params.get("DOMAIN") or "").strip()
    )


def _bitrix_auth_block(body: dict | None = None) -> dict:
    if not isinstance(body, dict):
        return {}
    auth = body.get("auth") or body.get("AUTH")
    return auth if isinstance(auth, dict) else {}


def _bitrix_domain_from_request(request: Request, body: dict | None = None) -> str:
    auth = _bitrix_auth_block(body)
    source = body if isinstance(body, dict) else {}
    raw_domain = (
        source.get("domain")
        or source.get("DOMAIN")
        or auth.get("domain")
        or auth.get("DOMAIN")
        or request.query_params.get("domain")
        or request.query_params.get("DOMAIN")
        or ""
    )
    return _normalize_bitrix_domain(str(raw_domain)) if raw_domain else ""


def _bitrix_access_token_from_request(request: Request, body: dict | None = None) -> str:
    auth = _bitrix_auth_block(body)
    source = body if isinstance(body, dict) else {}
    return str(
        source.get("AUTH_ID")
        or source.get("auth_id")
        or source.get("access_token")
        or source.get("ACCESS_TOKEN")
        or auth.get("AUTH_ID")
        or auth.get("auth_id")
        or auth.get("access_token")
        or auth.get("ACCESS_TOKEN")
        or request.query_params.get("AUTH_ID")
        or request.query_params.get("auth_id")
        or request.query_params.get("access_token")
        or request.query_params.get("ACCESS_TOKEN")
        or ""
    ).strip()


def _require_widget_auth(request: Request, domain: str) -> None:
    if check_admin_auth(request):
        return
    token = request.headers.get("X-SAFY-Widget-Token") or request.query_params.get("widget_token")
    if not _verify_widget_token(token or "", domain):
        raise HTTPException(status_code=403, detail="Invalid widget token")


async def _telegram_account_for_link(session, tenant_id: int, link: ChatLink | None) -> TelegramAccount | None:
    """Resolve the Telegram account that owns a concrete Open Line dialog.

    Bitrix can send all Open Line events under the same connector id, so connector/line
    are not enough when several Telegram accounts share one line. The reliable marker is
    the account_id saved on inbound/outbound messages for the same Telegram chat.
    """
    states = ["active", "authorized"]
    if link:
        if getattr(link, "account_id", None):
            account = (await session.execute(
                select(TelegramAccount).where(
                    TelegramAccount.id == int(link.account_id),
                    TelegramAccount.tenant_id == tenant_id,
                    TelegramAccount.state.in_(states),
                ).limit(1)
            )).scalar_one_or_none()
            if account:
                return account

        chat_scopes = []
        if link.id:
            chat_scopes.append(Message.chat_link_id == link.id)
        if link.external_chat_id:
            chat_scopes.append(Message.chat_id_tg == link.external_chat_id)
        if chat_scopes:
            for direction in ("inbound", ""):
                filters = [
                    Message.tenant_id == tenant_id,
                    Message.account_id.isnot(None),
                    or_(*chat_scopes),
                ]
                if direction:
                    filters.append(Message.direction == direction)
                account_id = (await session.execute(
                    select(Message.account_id)
                    .where(*filters)
                    .order_by(Message.created_at.desc(), Message.id.desc())
                    .limit(1)
                )).scalar_one_or_none()
                if account_id:
                    account = (await session.execute(
                        select(TelegramAccount).where(
                            TelegramAccount.id == int(account_id),
                            TelegramAccount.tenant_id == tenant_id,
                            TelegramAccount.state.in_(states),
                        ).limit(1)
                    )).scalar_one_or_none()
                    if account:
                        return account

        if link.bitrix_line_id:
            accounts = (await session.execute(
                select(TelegramAccount).where(
                    TelegramAccount.tenant_id == tenant_id,
                    TelegramAccount.bitrix_line_id == link.bitrix_line_id,
                    TelegramAccount.state.in_(states),
                ).order_by(TelegramAccount.id.asc()).limit(2)
            )).scalars().all()
            if len(accounts) == 1:
                return accounts[0]
            if len(accounts) > 1:
                logger.warning(
                    "Ambiguous Telegram account for tenant=%s line=%s link=%s; refusing to guess",
                    tenant_id, link.bitrix_line_id, link.id,
                )
                return None

    accounts = (await session.execute(
        select(TelegramAccount).where(
            TelegramAccount.tenant_id == tenant_id,
            TelegramAccount.bitrix_line_id == _primary_line_id(),
            TelegramAccount.state.in_(states),
        ).order_by(TelegramAccount.id.asc()).limit(2)
    )).scalars().all()
    if len(accounts) == 1:
        return accounts[0]
    if len(accounts) > 1:
        logger.warning("Ambiguous primary Telegram account for tenant=%s; refusing to guess", tenant_id)
    return None


async def _forward_bitrix_typing_to_telegram(domain: str, data: dict) -> dict:
    chat_id = _typing_chat_id(data)
    if not domain or not chat_id:
        return {"success": False, "message": "domain or chat_id missing"}
    async with async_session() as session:
        tenant = (await session.execute(select(Tenant).where(Tenant.bitrix_domain == domain))).scalar_one_or_none()
        if not tenant:
            return {"success": False, "message": "tenant not found"}
        link = (await session.execute(
            select(ChatLink).where(
                ChatLink.tenant_id == tenant.id,
                ChatLink.bitrix_chat_id == str(chat_id),
            ).order_by(ChatLink.updated_at.desc()).limit(1)
        )).scalar_one_or_none()
        if not link:
            return {"success": False, "message": "chat link not found"}
        account = await _telegram_account_for_link(session, tenant.id, link)
        account_id = account.id if account else None
    if not account_id:
        return {"success": False, "message": "telegram account not found"}
    from app.telegram.service import send_typing_action
    return await send_typing_action(account_id, link.external_chat_id)


def _normalize_widget_recipient(value: str) -> str:
    recipient = (value or "").strip()
    if not recipient:
        return ""

    # Operators often paste "t.me/name" links or type "@ name".
    recipient = _re.sub(r"^https?://", "", recipient, flags=_re.IGNORECASE)
    recipient = _re.sub(r"^(?:www\.)?(?:t\.me|telegram\.me)/", "@", recipient, flags=_re.IGNORECASE)
    recipient = recipient.split("?", 1)[0].split("#", 1)[0].strip().strip("/")

    if recipient.startswith("@"):
        username = _re.sub(r"\s+", "", recipient[1:])
        return f"@{username}" if username else ""
    if recipient.startswith("+"):
        phone = _re.sub(r"[\s().-]+", "", recipient)
        return phone
    if _re.match(r"^[A-Za-z0-9_ ]+$", recipient):
        return "@" + _re.sub(r"\s+", "", recipient)
    return recipient


def _bitrix_external_user_from_recipient(recipient: str, tg_user_id) -> dict:
    raw_name = (recipient or "").lstrip("@+").strip() or "Telegram user"
    safe_name = _re.sub(r"[^A-Za-zА-Яа-яЁё\s'-]", " ", raw_name).strip()
    safe_name = _re.sub(r"\s+", " ", safe_name)[:25].strip() or "Telegram user"
    user = {"id": str(tg_user_id), "name": safe_name}
    if recipient.startswith("@") and len(recipient) > 1:
        user["url"] = f"https://t.me/{recipient[1:]}"
    elif recipient.startswith("+"):
        user["phone"] = recipient
    return user


def _safe_json_object(value) -> dict:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            logger.warning("Could not parse JSON object from placement options")
    return {}


def _safe_file_name(name: str) -> str:
    name = (name or "file").replace("\\", "_").replace("/", "_").strip()
    return name[:120] or "file"


def _file_type_from_name(name: str = "", raw_type: str = "") -> str:
    if str(raw_type).lower() == "image":
        return "image"
    ext = (name or "").rsplit(".", 1)[-1].lower() if "." in (name or "") else ""
    return "image" if ext in {"jpg", "jpeg", "png", "gif", "webp", "bmp"} else "file"


def _normalize_file_entries(files) -> list[dict]:
    normalized = []
    if not isinstance(files, list):
        return normalized
    for item in files:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("NAME") or "file"
        url = (
            item.get("url") or item.get("URL") or item.get("link") or item.get("LINK") or
            item.get("downloadLink") or item.get("downloadUrl") or item.get("DOWNLOAD_URL") or ""
        )
        file_type = _file_type_from_name(name, item.get("type") or item.get("TYPE") or item.get("content_type") or "")
        normalized.append({
            "name": name,
            "url": url,
            "type": file_type,
            "size": item.get("size") or item.get("SIZE"),
        })
    return normalized


def _extract_files_from_raw(raw_payload_json: str) -> list[dict]:
    if not raw_payload_json:
        return []
    try:
        raw = json.loads(raw_payload_json)
    except Exception:
        return []
    candidates = []
    if isinstance(raw, dict):
        candidates.extend(_normalize_file_entries(raw.get("files")))
        message = raw.get("message")
        if isinstance(message, dict):
            candidates.extend(_normalize_file_entries(message.get("files")))
    return candidates


def _files_label(files: list[dict]) -> str:
    if not files:
        return ""
    labels = []
    for item in files:
        prefix = "Фото" if item.get("type") == "image" else "Файл"
        labels.append(f"[{prefix}: {item.get('name') or 'file'}]")
    return " ".join(labels)


def _decode_widget_files(files: list) -> list[dict]:
    decoded = []
    if not isinstance(files, list):
        return decoded
    for item in files[:5]:
        if not isinstance(item, dict):
            continue
        filename = _safe_file_name(item.get("name") or item.get("filename"))
        data = item.get("data_base64") or item.get("content")
        if not data:
            continue
        if isinstance(data, str) and "," in data and data.strip().lower().startswith("data:"):
            data = data.split(",", 1)[1]
        try:
            file_bytes = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            logger.warning("Widget file skipped: invalid base64 payload")
            continue
        if not file_bytes or len(file_bytes) > int(settings.max_telegram_media_bytes or 25 * 1024 * 1024):
            logger.warning("Widget file skipped: empty or too large")
            continue
        decoded.append({
            "name": filename,
            "bytes": file_bytes,
            "content_type": item.get("content_type") or item.get("type") or "",
        })
    return decoded


def _message_ids_from_result(result: dict) -> list[str]:
    if not isinstance(result, dict):
        return []
    raw_ids = result.get("message_ids")
    if not raw_ids:
        raw_ids = [result.get("message_id")] if result.get("message_id") else []
    return [str(mid) for mid in raw_ids if mid is not None and str(mid) != ""]


def _media_limit_bytes() -> int:
    return max(1, int(settings.max_telegram_media_bytes or 1024 * 1024 * 1024))


async def _download_url_limited(
    url: str,
    headers: dict | None = None,
    max_bytes: int | None = None,
    filename: str | None = None,
) -> dict:
    import httpx

    limit = max(1, int(max_bytes or _media_limit_bytes()))
    tmp_path = ""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as http:
            async with http.stream("GET", url, headers=headers or {}) as resp:
                if resp.status_code != 200:
                    return {"success": False, "error": f"HTTP {resp.status_code}", "status_code": resp.status_code}
                content_length = resp.headers.get("content-length")
                if content_length:
                    try:
                        expected_size = int(content_length)
                    except (TypeError, ValueError):
                        expected_size = 0
                    if expected_size > limit:
                        return {
                            "success": False,
                            "too_large": True,
                            "size": expected_size,
                            "limit": limit,
                            "error": f"File is too large: {expected_size} bytes exceeds {limit} bytes",
                        }

                suffix = os.path.splitext(_safe_file_name(filename or ""))[1].lower()
                if not suffix or len(suffix) > 12 or not suffix.startswith("."):
                    suffix = ".download"
                with tempfile.NamedTemporaryFile(delete=False, prefix="bitrix_file_", suffix=suffix) as tmp:
                    tmp_path = tmp.name
                    total = 0
                    async for chunk in resp.aiter_bytes():
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > limit:
                            tmp.close()
                            try:
                                os.remove(tmp_path)
                            except FileNotFoundError:
                                pass
                            return {
                                "success": False,
                                "too_large": True,
                                "size": total,
                                "limit": limit,
                                "error": f"File is too large: more than {limit} bytes",
                            }
                        tmp.write(chunk)
        return {"success": True, "path": tmp_path, "size": os.path.getsize(tmp_path)}
    except Exception as exc:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except FileNotFoundError:
                pass
        return {"success": False, "error": str(exc)}


async def _update_message_status(message_db_id, status: str, error: str = None):
    if not message_db_id:
        return
    try:
        async with async_session() as session:
            values = {"status": status, "error": error}
            await session.execute(update(Message).where(Message.id == message_db_id).values(**values))
            await session.commit()
    except Exception as e:
        logger.warning(f"Could not update message {message_db_id} status to {status}: {e}")


async def _mark_external_delivery(payload: dict, result: dict):
    message_db_id = payload.get("message_db_id")
    message_ids = result.get("message_ids") or ([result.get("message_id")] if result.get("message_id") else [])
    message_ids = [str(mid) for mid in message_ids if mid]
    if message_db_id and message_ids:
        try:
            async with async_session() as session:
                await session.execute(
                    update(Message).where(Message.id == message_db_id).values(
                        provider_message_id=",".join(message_ids),
                    )
                )
                await session.commit()
        except Exception as e:
            logger.warning(f"Could not save provider message ids for message {message_db_id}: {e}")

    if not result.get("success") or not message_ids:
        return
    im = payload.get("im")
    domain = payload.get("domain")
    token_enc = payload.get("token_enc")
    connector_id = payload.get("connector_id")
    line_id = payload.get("line_id")
    delivery_chat_id = payload.get("delivery_chat_id") or payload.get("external_chat_id")
    if not (im and domain and token_enc and connector_id and line_id and delivery_chat_id):
        return
    try:
        await bx.send_delivery(domain, token_enc, connector_id, int(line_id), im, message_ids, str(delivery_chat_id))
    except Exception as e:
        logger.warning(f"send_delivery after external send failed: {e}")


async def _ensure_lead_for_link(domain: str, token_enc: str, link_id: int,
                                connector_id: str, display_name: str = "",
                                recipient: str = "", first_text: str = "",
                                assigned_by_id=None) -> dict:
    if not (domain and token_enc and link_id and connector_id):
        return {"success": False, "error": "missing lead context"}
    async with async_session() as session:
        link = await session.get(ChatLink, link_id)
        if not link:
            return {"success": False, "error": "chat link not found"}
        if link.crm_entity_type == "lead" and link.crm_entity_id:
            return {"success": True, "created": False, "id": link.crm_entity_id, "already_linked": True}
        external_chat_id = link.external_chat_id
        bitrix_chat_id = link.bitrix_chat_id
        name = display_name or link.external_user_name or external_chat_id

    result = await create_or_get_lead(
        domain=domain,
        token_enc=token_enc,
        connector_id=connector_id,
        external_chat_id=external_chat_id,
        display_name=name,
        recipient=recipient,
        first_text=first_text,
        bitrix_chat_id=bitrix_chat_id,
        assigned_by_id=assigned_by_id,
    )
    if result.get("success") and result.get("id"):
        async with async_session() as session:
            await session.execute(
                update(ChatLink).where(ChatLink.id == link_id).values(
                    crm_entity_type="lead",
                    crm_entity_id=str(result["id"]),
                    updated_at=utcnow(),
                )
            )
            await session.commit()
    return result


async def _send_to_external_handler(payload: dict) -> dict:
    account_id = payload.get("account_id")
    external_chat_id = payload.get("external_chat_id")
    text = payload.get("text", "")
    files = payload.get("files", [])
    message_db_id = payload.get("message_db_id")

    # Telegram: send via active Telethon client
    if account_id is not None:
        from app.telegram.service import send_by_entity, send_file
        # Prefer @username if available (avoids entity-not-found after restart)
        tg_recipient = payload.get("tg_recipient")
        if tg_recipient:
            chat_target = tg_recipient
        else:
            try:
                chat_target = int(external_chat_id)
            except (TypeError, ValueError):
                chat_target = external_chat_id

        # Send files first if any
        any_file_sent = False
        sent_message_ids = []
        transfer_errors = []
        for file_info in files:
            if not isinstance(file_info, dict):
                continue
            # Prefer downloadLink (signed, no auth needed) over short link
            file_url = (file_info.get("downloadLink") or
                        file_info.get("downloadUrl") or
                        file_info.get("DOWNLOAD_URL") or
                        file_info.get("link") or file_info.get("LINK") or
                        file_info.get("url") or file_info.get("URL") or "")
            file_name = file_info.get("name") or file_info.get("NAME", "file")
            if not file_url:
                continue
            try:
                is_first = not any_file_sent
                headers = {}
                token_enc = payload.get("token_enc")
                if token_enc:
                    try:
                        headers["Authorization"] = f"Bearer {decrypt_token(token_enc)}"
                    except Exception:
                        logger.warning("Could not decrypt token for Bitrix24 file download")
                downloaded = await _download_url_limited(
                    file_url,
                    headers=headers,
                    max_bytes=_media_limit_bytes(),
                    filename=file_name,
                )
                if not downloaded.get("success"):
                    error_text = downloaded.get("error") or "download failed"
                    transfer_errors.append(f"{file_name}: {error_text}")
                    logger.warning("Could not download file %s: %s", file_url, error_text)
                    continue
                file_payload = downloaded.get("path") or downloaded.get("bytes")
                try:
                    result = await send_file(int(account_id), chat_target, file_payload, file_name,
                                             caption=text if is_first else "")
                finally:
                    if downloaded.get("path"):
                        try:
                            os.remove(downloaded["path"])
                        except FileNotFoundError:
                            pass
                if result.get("success"):
                    any_file_sent = True
                    sent_message_ids.extend(_message_ids_from_result(result))
                else:
                    error_text = result.get("error") or "send failed"
                    transfer_errors.append(f"{file_name}: {error_text}")
                    logger.warning(f"send_file failed: {error_text}")
            except Exception as e:
                logger.error(f"Error sending file to Telegram: {e}")
                transfer_errors.append(f"{file_name}: {e}")

        # Send text if no files or text alongside files was handled via caption
        if text and not files:
            result = await send_by_entity(int(account_id), chat_target, text)
            sent_message_ids.extend(_message_ids_from_result(result))
        elif text and files and not any_file_sent:
            result = await send_by_entity(int(account_id), chat_target, text)
            sent_message_ids.extend(_message_ids_from_result(result))
        elif files:
            result = {
                "success": any_file_sent,
                "error": None if any_file_sent else ("; ".join(transfer_errors) or "All file downloads failed"),
            }
        else:
            result = await send_by_entity(int(account_id), chat_target, text)
            sent_message_ids.extend(_message_ids_from_result(result))
        if sent_message_ids:
            result["message_ids"] = sent_message_ids
            result["message_id"] = sent_message_ids[0]
        if transfer_errors:
            result["partial_errors"] = (result.get("partial_errors") or []) + transfer_errors
            if not result.get("success") and not result.get("error"):
                result["error"] = "; ".join(transfer_errors)
        status = "sent" if result.get("success") else "failed"
        error = None if result.get("success") else result.get("error")
        if result.get("success") and result.get("partial_errors"):
            status = "partial_failed"
            error = "; ".join(str(item) for item in result.get("partial_errors", [])[:5])
        await _update_message_status(
            message_db_id,
            status,
            error,
        )
        await _mark_external_delivery(payload, result)
        return result

    # HTTP provider
    provider_key = payload.get("provider_key")
    channel_id = payload.get("channel_id")
    provider = get_provider(provider_key)
    if not provider:
        await _update_message_status(message_db_id, "failed", f"No provider for {provider_key}")
        return {"success": False, "error": f"No provider for {provider_key}"}
    result = await provider.send_message(
        channel_id=str(channel_id),
        external_chat_id=external_chat_id,
        text=text,
    )
    await _update_message_status(
        message_db_id,
        "sent" if result.get("success") else "failed",
        None if result.get("success") else result.get("error"),
    )
    return result


outbox.register_handler("to_external", _send_to_external_handler)


@router.api_route("/bitrix/install", methods=["GET", "POST"])
async def bitrix_install(request: Request):
    query = dict(request.query_params)

    # OAuth callback: Bitrix24 redirects here with ?code=...
    if request.method == "GET" and query.get("code"):
        from app.config import settings as s
        from pybitrix24 import Bitrix24
        raw_domain = query.get("DOMAIN") or query.get("domain", "")
        if not raw_domain:
            return HTMLResponse("<html><body><h1>Error</h1><p>Missing DOMAIN parameter</p></body></html>",
                                status_code=400)
        domain = _normalize_bitrix_domain(raw_domain)
        try:
            bx24 = Bitrix24(domain, s.bitrix24_client_id, s.bitrix24_client_secret)
            tokens = bx24.obtain_tokens(query["code"])
            access_token = tokens.get("access_token", "")
            refresh_token = tokens.get("refresh_token", "")
            expires_in = tokens.get("expires_in")
            install_user_id = tokens.get("user_id") or query.get("AUTH_USER_ID")

            enc_token = encrypt_token(access_token)
            enc_refresh = encrypt_token(refresh_token) if refresh_token else None
            connector_id = _primary_connector_id(s.connector_id)
            expires_at = None
            if expires_in:
                expires_at = utcnow() + timedelta(seconds=int(expires_in))

            async with async_session() as session:
                existing = await session.execute(select(Tenant).where(Tenant.bitrix_domain == domain))
                tenant = existing.scalar_one_or_none()
                if tenant:
                    await session.execute(update(Tenant).where(Tenant.id == tenant.id).values(
                        access_token_encrypted=enc_token,
                        refresh_token_encrypted=enc_refresh or tenant.refresh_token_encrypted,
                        expires_at=expires_at,
                        install_user_id=int(install_user_id) if install_user_id else tenant.install_user_id,
                    ))
                else:
                    tenant = Tenant(
                        bitrix_domain=domain,
                        access_token_encrypted=enc_token,
                        refresh_token_encrypted=enc_refresh,
                        expires_at=expires_at,
                        install_user_id=int(install_user_id) if install_user_id else None,
                    )
                    session.add(tenant)
                await session.commit()

                existing_conn = await session.execute(
                    select(Connector).where(Connector.tenant_id == tenant.id,
                                           Connector.connector_id == connector_id)
                )
                if not existing_conn.scalar_one_or_none():
                    conn = Connector(tenant_id=tenant.id, connector_id=connector_id, name=s.connector_name)
                    session.add(conn)
                    await session.commit()

            try:
                await bx.register_connector(domain, enc_token, connector_id, s.connector_name,
                                            f"{s.app_url}/bitrix/install")
                await bx.bind_event(domain, enc_token, "ONIMCONNECTORMESSAGEADD", f"{s.app_url}/bitrix/events")
                await bx.bind_event(domain, enc_token, "ONIMCONNECTORMESSAGEUPDATE", f"{s.app_url}/bitrix/events")
                await bx.bind_event(domain, enc_token, "ONIMCONNECTORSTATUSDELETE", f"{s.app_url}/bitrix/events")
                await bx.bind_event(domain, enc_token, "ONIMCONNECTORLINEDELETE", f"{s.app_url}/bitrix/events")
            except Exception as e:
                logger.warning(f"Bitrix24 API calls failed during OAuth install: {e}")

            return HTMLResponse(
                content=f"<html><body><h1>Installed!</h1><p>Tokens saved for {html_lib.escape(domain)}</p></body></html>"
            )
        except Exception as e:
            return HTMLResponse(
                content=f"<html><body><h1>Error</h1><p>{html_lib.escape(str(e))}</p></body></html>",
                status_code=500,
            )

    # POST install from Bitrix24 marketplace
    body = {}
    if request.method == "POST":
        try:
            raw = await _read_limited_body(request, _max_body_bytes())
            raw_str = raw.decode("utf-8", errors="replace")
            if "application/json" in request.headers.get("content-type", ""):
                body = json.loads(raw_str) if raw_str else {}
            else:
                body = _parse_php_form(raw_str) if raw_str else {}
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"bitrix_install: could not parse POST body: {e}")
            body = {}

    raw_domain = body.get("DOMAIN") or query.get("DOMAIN")
    domain = _normalize_bitrix_domain(raw_domain) if raw_domain else ""
    access_token = body.get("AUTH_ID")
    refresh_token = body.get("REFRESH_ID")
    member_id = body.get("member_id")
    install_user_id = body.get("AUTH_USER_ID") or query.get("AUTH_USER_ID")
    expires_in = body.get("EXPIRES_IN") or query.get("EXPIRES_IN")
    placement = body.get("PLACEMENT") or query.get("PLACEMENT")
    placement_options = body.get("PLACEMENT_OPTIONS") or query.get("PLACEMENT_OPTIONS")

    if not domain:
        return HTMLResponse("<html><body><h1>Error: missing DOMAIN</h1></body></html>", status_code=400)

    if not access_token or not await _valid_bitrix_access_token(domain, str(access_token)):
        logger.warning(
            "bitrix_install: rejected unauthenticated POST placement=%r domain=%r",
            placement,
            domain,
        )
        raise HTTPException(status_code=403, detail="Invalid Bitrix install token")

    if placement == "SETTING_CONNECTOR":
        if not placement_options:
            placement_options = query.get("PLACEMENT_OPTIONS", "{}")
        return await handle_connector_settings(domain, str(access_token), placement_options)

    expires_at = None
    if expires_in:
        try:
            expires_at = utcnow() + timedelta(seconds=int(expires_in))
        except Exception:
            pass

    enc_token = encrypt_token(access_token)
    enc_refresh = encrypt_token(refresh_token) if refresh_token else None
    connector_id = _primary_connector_id()

    async with async_session() as session:
        result = await session.execute(select(Tenant).where(Tenant.bitrix_domain == domain))
        tenant = result.scalar_one_or_none()
        if tenant:
            await session.execute(
                update(Tenant).where(Tenant.id == tenant.id).values(
                    access_token_encrypted=enc_token,
                    refresh_token_encrypted=enc_refresh or tenant.refresh_token_encrypted,
                    member_id=member_id or tenant.member_id,
                    expires_at=expires_at or tenant.expires_at,
                    install_user_id=int(install_user_id) if install_user_id else tenant.install_user_id,
                )
            )
        else:
            tenant = Tenant(
                bitrix_domain=domain,
                member_id=member_id,
                access_token_encrypted=enc_token,
                refresh_token_encrypted=enc_refresh,
                expires_at=expires_at,
                install_user_id=int(install_user_id) if install_user_id else None,
            )
            session.add(tenant)
        await session.commit()

        result = await session.execute(
            select(Connector).where(Connector.tenant_id == tenant.id,
                                   Connector.connector_id == connector_id)
        )
        if not result.scalar_one_or_none():
            session.add(Connector(tenant_id=tenant.id, connector_id=connector_id,
                                  name=settings.connector_name))
            await session.commit()

    try:
        await bx.register_connector(domain, enc_token, connector_id, settings.connector_name,
                                    f"{settings.app_url}/bitrix/install")
        await bx.bind_event(domain, enc_token, "ONIMCONNECTORMESSAGEADD", f"{settings.app_url}/bitrix/events")
        await bx.bind_event(domain, enc_token, "ONIMCONNECTORMESSAGEUPDATE", f"{settings.app_url}/bitrix/events")
        await bx.bind_event(domain, enc_token, "ONIMCONNECTORSTATUSDELETE", f"{settings.app_url}/bitrix/events")
        await bx.bind_event(domain, enc_token, "ONIMCONNECTORLINEDELETE", f"{settings.app_url}/bitrix/events")
    except Exception as e:
        logger.warning(f"Bitrix24 API calls failed (will retry on next install): {e}")

    logger.info(f"App installed for {domain}")
    return HTMLResponse(
        content="""<!DOCTYPE html><html><head><title>Installed</title>
<script src="//api.bitrix24.com/api/v1/"></script></head>
<body style="font-family:Arial;padding:40px;text-align:center;">
<div style="background:#d4edda;color:#155724;padding:20px;border-radius:8px;">SAFY Telegram Installed!</div>
<script>if(typeof BX24!==undefined)BX24.init(()=>BX24.installFinish());</script></body></html>""",
        headers={"X-Frame-Options": "ALLOWALL", "Content-Security-Policy": "frame-ancestors *"},
    )


async def handle_connector_settings(domain, access_token, placement_options):
    if not domain or not access_token:
        raise HTTPException(status_code=403, detail="Invalid Bitrix install token")
    options = _safe_json_object(placement_options)
    line = int(options.get("LINE", 0))
    active = int(options.get("ACTIVE_STATUS", 1))
    connector_id = _primary_connector_id()
    primary_line = _primary_line_id(line)
    enc_token = encrypt_token(access_token)

    async with async_session() as session:
        tenant_result = await session.execute(select(Tenant).where(Tenant.bitrix_domain == domain))
        tenant = tenant_result.scalar_one_or_none()

    if line and line != primary_line:
        for old_connector in {settings.connector_id, connector_id}:
            try:
                await bx.activate_connector(domain, enc_token, old_connector, line, 0)
            except Exception as e:
                logger.warning(f"deactivate non-primary line failed: connector={old_connector} line={line}: {e}")
        return Response(content="successfully")

    try:
        result = await bx.activate_connector(domain, enc_token, connector_id, primary_line, active)
    except Exception as e:
        logger.warning(f"activate failed: {e}")
        return Response(content="successfully")

    if active and result is not None:
        data_id = f"{connector_id}_line_{primary_line}"
        await bx.set_connector_data(domain, enc_token, connector_id, primary_line, {
            "ID": data_id,
            "NAME": settings.connector_name,
            "URL": f"{settings.app_url}/bitrix/install",
            "URL_IM": f"{settings.app_url}/bitrix/widget",
        })

        async with async_session() as session:
            result2 = await session.execute(select(Tenant).where(Tenant.bitrix_domain == domain))
            tenant = result2.scalar_one_or_none()
            if tenant:
                existing_ch = await session.execute(
                    select(Channel).where(Channel.tenant_id == tenant.id,
                                         Channel.bitrix_line_id == primary_line,
                                         Channel.provider == connector_id)
                )
                if not existing_ch.scalar_one_or_none():
                    ch = Channel(
                        tenant_id=tenant.id, provider=connector_id,
                        name=f"Line {primary_line}", bitrix_line_id=primary_line, status="active",
                    )
                    session.add(ch)
                    await session.commit()

    return Response(content="successfully")


def _parse_php_form(raw: str) -> dict:
    """Parse PHP-style form-encoded body (data[key][0]=val) into nested dict."""
    from urllib.parse import parse_qs
    import re
    flat = parse_qs(raw, keep_blank_values=True)
    if len(flat) > 2000:
        raise ValueError("Too many form fields")
    result: dict = {}
    for key, vals in flat.items():
        val = vals[0] if len(vals) == 1 else vals
        parts = re.split(r'\[|\]', key)
        parts = [p for p in parts if p != ""]
        if not parts:
            continue
        if len(parts) > 12:
            raise ValueError("Form nesting is too deep")
        node = result
        for i, part in enumerate(parts[:-1]):
            nxt = parts[i + 1]
            if nxt.isdigit():
                idx = int(nxt)
                if idx > 500:
                    raise ValueError("Form list index is too large")
                node = node.setdefault(part, [])
                while len(node) <= idx:
                    node.append({})
                node = node[idx]
            else:
                if not part.isdigit():
                    node = node.setdefault(part, {})
        last = parts[-1]
        if isinstance(node, dict):
            node[last] = val
    return result


def _valid_hmac_signature(raw_bytes: bytes, header_value: str, secret: str) -> bool:
    if not raw_bytes or not header_value or not secret:
        return False
    digest = hmac.new(secret.encode(), raw_bytes, hashlib.sha256).digest()
    expected_values = {
        digest.hex(),
        base64.b64encode(digest).decode(),
        base64.urlsafe_b64encode(digest).decode().rstrip("="),
    }
    provided = header_value.strip()
    return any(hmac.compare_digest(provided, expected) for expected in expected_values)


def _verify_bitrix_webhook(request: Request, raw_bytes: bytes, body: dict) -> bool:
    secrets = [value for value in {settings.bitrix_webhook_token, settings.bitrix24_client_secret} if value]
    hmac_header = request.headers.get("X-Bitrix-Hmac-Sha256") or request.headers.get("X-Bitrix-Hmac-SHA256")
    if hmac_header:
        return any(_valid_hmac_signature(raw_bytes, hmac_header, secret) for secret in secrets)

    auth = body.get("auth", {}) if isinstance(body, dict) else {}
    if isinstance(auth, dict):
        app_token = auth.get("application_token") or auth.get("APPLICATION_TOKEN")
        if app_token and settings.bitrix_webhook_token:
            return hmac.compare_digest(str(app_token), settings.bitrix_webhook_token)

    return not settings.bitrix_webhook_hmac_required


@router.post("/bitrix/events")
async def bitrix_events(request: Request):
    content_type = request.headers.get("content-type", "")
    raw_bytes = await _read_limited_body(request, _max_body_bytes())
    raw_str = raw_bytes.decode("utf-8", errors="replace")

    body: dict = {}
    if "application/json" in content_type:
        try:
            body = json.loads(raw_str)
        except Exception:
            pass
    else:
        try:
            body = _parse_php_form(raw_str)
        except Exception as e:
            logger.warning(f"bitrix_events: form parse error: {e}, raw={raw_str[:300]}")

    if not _verify_bitrix_webhook(request, raw_bytes, body):
        logger.warning("bitrix_events: rejected request with invalid signature/token")
        raise HTTPException(status_code=403, detail="Invalid Bitrix webhook signature")

    event = body.get("event", "")
    auth = body.get("auth", {})
    if isinstance(auth, list):
        auth = {}
    domain = auth.get("domain", "")
    data = body.get("data", {})
    if isinstance(data, list):
        data = {}

    connector = data.get("CONNECTOR", "")
    logger.info(f"bitrix_events: event={event!r} domain={domain!r} connector={connector!r} ct={content_type!r}")

    if event.upper() == "ONIMLIVECHATSTATUS":
        return _record_typing_status(domain, data)
    if event.upper() == "ONIMCHATUSERTYPING":
        _record_typing_status(domain, data)
        return await _forward_bitrix_typing_to_telegram(domain, data)

    # This installation is intentionally bound to one Telegram connector and one Open Line.
    if connector and connector != _primary_connector_id():
        logger.info(f"bitrix_events: ignored non-primary connector {connector!r}")
        return {"success": True, "ignored": True}

    if event == "ONIMCONNECTORMESSAGEADD":
        return await handle_operator_reply(domain, data, connector)
    if event == "ONIMCONNECTORMESSAGEUPDATE":
        return await handle_operator_edit(domain, data, connector)
    elif event in ("ONIMCONNECTORSTATUSDELETE", "ONIMCONNECTORLINEDELETE",
                   "OnImConnectorStatusDelete", "OnImConnectorLineDelete"):
        line = data.get("line") or data.get("LINE")
        if domain and line:
            async with async_session() as session:
                await session.execute(delete(Channel).where(
                    Channel.tenant_id.in_(
                        select(Tenant.id).where(Tenant.bitrix_domain == domain)
                    ),
                    Channel.bitrix_line_id == int(line),
                ))
                await session.commit()
        return {"success": True}

    return {"success": True}


def _as_list(value) -> list:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    return [value]


async def handle_operator_edit(domain: str, data: dict, connector_id: str = None):
    messages = data.get("MESSAGES", [])
    logger.info(
        "handle_operator_edit: domain=%r connector=%r messages_count=%s",
        domain, connector_id, len(messages) if isinstance(messages, list) else type(messages).__name__,
    )
    if not messages:
        return {"success": True}

    async with async_session() as session:
        result = await session.execute(select(Tenant).where(Tenant.bitrix_domain == domain))
        tenant = result.scalar_one_or_none()
        if not tenant:
            logger.warning(f"handle_operator_edit: tenant not found for domain {domain}")
            return {"success": False, "message": "Tenant not found"}

        for msg_index, msg in enumerate(messages):
            im = msg.get("im", {}) or {}
            message = msg.get("message", {}) or {}
            chat = msg.get("chat", {}) or {}
            text = message.get("text", "")
            _, clean_text = _parse_bitrix_text(text)
            external_chat_id = str(chat.get("id", ""))
            bitrix_internal_chat_id = str(im.get("chat_id") or im.get("CHAT_ID") or "")
            external_message_ids = [str(mid) for mid in _as_list(message.get("id")) if mid]
            im_message_id = str(im.get("message_id") or im.get("MESSAGEID") or "")

            link = None
            link_ambiguous = False
            if bitrix_internal_chat_id:
                q = select(ChatLink).where(
                    ChatLink.tenant_id == tenant.id,
                    ChatLink.bitrix_chat_id == bitrix_internal_chat_id,
                )
                if connector_id:
                    q = q.where(ChatLink.provider == connector_id)
                link = (await session.execute(q.order_by(ChatLink.id.desc()).limit(1))).scalars().first()
            if external_chat_id:
                if not link:
                    q = select(ChatLink).where(
                        ChatLink.tenant_id == tenant.id,
                        ChatLink.external_chat_id == external_chat_id,
                    )
                    if connector_id:
                        q = q.where(ChatLink.provider == connector_id)
                    candidates = (await session.execute(q.order_by(ChatLink.id.desc()).limit(2))).scalars().all()
                    if len(candidates) == 1:
                        link = candidates[0]
                    elif len(candidates) > 1:
                        link_ambiguous = True
                if not link and not link_ambiguous:
                    link = (await session.execute(
                        select(ChatLink).where(
                            ChatLink.tenant_id == tenant.id,
                            ChatLink.bitrix_chat_id == external_chat_id,
                        ).order_by(ChatLink.id.desc()).limit(1)
                    )).scalars().first()
            if not link:
                if link_ambiguous:
                    logger.warning(f"Edit skipped: ambiguous ChatLink for chat={external_chat_id} domain={domain}")
                else:
                    logger.warning(f"Edit skipped: no ChatLink for chat={external_chat_id} domain={domain}")
                continue

            if not external_message_ids and im_message_id:
                saved = (await session.execute(
                    select(Message).where(
                        Message.chat_link_id == link.id,
                        Message.bitrix_message_id == im_message_id,
                        Message.provider_message_id.isnot(None),
                    ).order_by(Message.id.desc()).limit(1)
                )).scalars().first()
                if saved and saved.provider_message_id:
                    external_message_ids = [mid for mid in saved.provider_message_id.split(",") if mid]

            tg_account = await _telegram_account_for_link(session, tenant.id, link)
            if not tg_account:
                logger.warning(f"Edit skipped: no Telegram account for link={link.id}")
                continue

            if not external_message_ids:
                status = "edit_failed"
                error = "No Telegram message ids saved for this Bitrix edit"
                text_hash = hashlib.md5(clean_text.encode("utf-8")).hexdigest()[:12]
                event_key = (
                    f"bitrix_edit_{domain}_{external_chat_id}_"
                    f"{im_message_id or 'no_provider_ids'}_{text_hash}"
                )
                existing = await session.execute(select(Message).where(Message.idempotency_key == event_key))
                if not existing.scalar_one_or_none():
                    session.add(Message(
                        tenant_id=tenant.id,
                        channel_id=link.channel_id,
                        chat_link_id=link.id,
                        direction="outbound",
                        bitrix_message_id=im_message_id or None,
                        provider_message_id=None,
                        text=clean_text,
                        status=status,
                        error=error,
                        raw_payload_json=json.dumps({"source": "bitrix_edit", **msg}, ensure_ascii=False),
                        idempotency_key=event_key,
                        account_id=tg_account.id,
                        chat_id_tg=link.external_chat_id,
                        sender_name="Bitrix edit",
                    ))
                    await session.commit()
                logger.warning("Edit skipped: %s link=%s im_message_id=%r", error, link.id, im_message_id)
                continue

            status = "edited"
            error = None
            edited_any = False
            from app.telegram.service import edit_message
            try:
                chat_target = int(link.external_chat_id)
            except (TypeError, ValueError):
                chat_target = link.external_chat_id
            for external_message_id in external_message_ids[:3]:
                result = await edit_message(tg_account.id, chat_target, external_message_id, clean_text)
                if result.get("success"):
                    edited_any = True
                else:
                    status = "edit_failed"
                    error = result.get("error")

            event_key = f"bitrix_edit_{domain}_{external_chat_id}_{im_message_id or '_'.join(external_message_ids)}"
            existing = await session.execute(select(Message).where(Message.idempotency_key == event_key))
            if not existing.scalar_one_or_none():
                session.add(Message(
                    tenant_id=tenant.id,
                    channel_id=link.channel_id,
                    chat_link_id=link.id,
                    direction="outbound",
                    bitrix_message_id=im_message_id or None,
                    provider_message_id=",".join(external_message_ids) if external_message_ids else None,
                    text=clean_text,
                    status=status if not edited_any else "edited",
                    error=error,
                    raw_payload_json=json.dumps({"source": "bitrix_edit", **msg}, ensure_ascii=False),
                    idempotency_key=event_key,
                    account_id=tg_account.id,
                    chat_id_tg=link.external_chat_id,
                    sender_name="Bitrix edit",
                ))
                await session.commit()

            if edited_any:
                try:
                    await bx.send_delivery(
                        domain,
                        tenant.access_token_encrypted,
                        connector_id or link.provider or _primary_connector_id(),
                        _primary_line_id(link.bitrix_line_id),
                        im,
                        external_message_ids,
                        external_chat_id,
                    )
                except Exception as e:
                    logger.warning(f"send_delivery for edit failed: {e}")

    return {"success": True}


async def handle_operator_reply(domain: str, data: dict, connector_id: str = None):
    messages = data.get("MESSAGES", [])
    logger.info(f"handle_operator_reply: domain={domain!r} connector={connector_id!r} messages_count={len(messages) if isinstance(messages, list) else type(messages).__name__} data_keys={list(data.keys()) if isinstance(data, dict) else 'N/A'}")
    if not messages:
        return {"success": True}

    async with async_session() as session:
        result = await session.execute(select(Tenant).where(Tenant.bitrix_domain == domain))
        tenant = result.scalar_one_or_none()
        if not tenant:
            logger.warning(f"handle_operator_reply: tenant not found for domain {domain}")
            return {"success": False, "message": "Tenant not found"}

        for msg_index, msg in enumerate(messages):
            im = msg.get("im", {})
            message = msg.get("message", {})
            chat = msg.get("chat", {})
            user = msg.get("user", {}) or {}
            text = message.get("text", "")
            operator_name, clean_text = _parse_bitrix_text(text)
            if not operator_name:
                operator_name = user.get("name") or (
                    f"Bitrix user #{user.get('id')}" if user.get("id") else ""
                )
            bitrix_chat_id = str(chat.get("id", ""))
            bitrix_internal_chat_id = str(im.get("chat_id") or im.get("CHAT_ID") or "")
            msg_ids = message.get("id", [])
            files = message.get("files", [])
            file_label = _files_label(_normalize_file_entries(files))

            # Prefer im.message_id as unique ID (Bitrix24 usually sends this).
            im_message_id = im.get("message_id") or im.get("MESSAGEID")
            if im_message_id:
                idempotency_key = f"bitrix_{domain}_{bitrix_chat_id}_{im_message_id}"
            elif msg_ids and msg_ids != []:
                idempotency_key = f"bitrix_{domain}_{bitrix_chat_id}_{str(msg_ids)}"
            else:
                fingerprint = hashlib.sha256(
                    json.dumps(
                        {
                            "text": text or "",
                            "files": files or [],
                            "user": user.get("id") or user.get("ID") or "",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ).encode("utf-8")
                ).hexdigest()[:16]
                event_marker = (
                    _data_get_any(data, "EVENT_ID", "event_id", "TS", "ts")
                    or message.get("date")
                    or message.get("DATE")
                    or time.time_ns()
                )
                idempotency_key = f"bitrix_{domain}_{bitrix_chat_id}_noid_{fingerprint}_{event_marker}_{msg_index}"

            existing = await session.execute(
                select(Message).where(Message.idempotency_key == idempotency_key)
            )
            if existing.scalar_one_or_none():
                continue

            # Find ChatLink: Bitrix24 echoes back the external chat.id we originally sent
            link = None
            link_ambiguous = False
            if bitrix_internal_chat_id:
                q = select(ChatLink).where(
                    ChatLink.tenant_id == tenant.id,
                    ChatLink.bitrix_chat_id == bitrix_internal_chat_id,
                )
                if connector_id:
                    q = q.where(ChatLink.provider == connector_id)
                link_result = await session.execute(q.order_by(ChatLink.id.desc()).limit(1))
                link = link_result.scalars().first()
            if bitrix_chat_id:
                # Primary: external_chat_id is what Bitrix24 sends in ONIMCONNECTORMESSAGEADD
                if not link:
                    q = select(ChatLink).where(
                        ChatLink.tenant_id == tenant.id,
                        ChatLink.external_chat_id == bitrix_chat_id,
                    )
                    if connector_id:
                        q = q.where(ChatLink.provider == connector_id)
                    q = q.order_by(ChatLink.bitrix_chat_id.is_(None), ChatLink.id.desc())
                    candidates = (await session.execute(q.limit(2))).scalars().all()
                    if len(candidates) == 1:
                        link = candidates[0]
                    elif len(candidates) > 1:
                        link_ambiguous = True
                # Fallback: by internal bitrix_chat_id (for HTTP connectors)
                if not link and not link_ambiguous:
                    link_result = await session.execute(
                        select(ChatLink).where(
                            ChatLink.tenant_id == tenant.id,
                            ChatLink.bitrix_chat_id == bitrix_chat_id,
                        ).order_by(ChatLink.id.desc())
                    )
                    link = link_result.scalars().first()

            # Find channel
            channel = None
            if connector_id:
                ch_result = await session.execute(
                    select(Channel).where(
                        Channel.tenant_id == tenant.id,
                        Channel.provider == connector_id,
                    )
                )
                channel = ch_result.scalars().first()
            if not channel:
                ch_result = await session.execute(
                    select(Channel).where(
                        Channel.tenant_id == tenant.id,
                        Channel.bitrix_line_id == _primary_line_id(),
                    )
                )
                channel = ch_result.scalars().first()

            tg_account = await _telegram_account_for_link(session, tenant.id, link)

            m = Message(
                tenant_id=tenant.id,
                channel_id=channel.id if channel else None,
                chat_link_id=link.id if link else None,
                direction="outbound",
                bitrix_message_id=str(im_message_id) if im_message_id else (str(msg_ids) if msg_ids else None),
                text=clean_text or file_label,
                sender_name=operator_name or None,
                status="queued",
                raw_payload_json=json.dumps(msg, ensure_ascii=False),
                idempotency_key=idempotency_key,
                account_id=tg_account.id if tg_account else None,
                chat_id_tg=link.external_chat_id if link else None,
            )
            session.add(m)
            await session.commit()
            await session.refresh(m)
            message_db_id = m.id

            if link and link.external_chat_id:
                telegram_connector = (connector_id or link.provider or "").startswith("tg_") or (
                    (connector_id or link.provider or "") == _primary_connector_id()
                )
                if not tg_account and telegram_connector:
                    await session.execute(
                        update(Message).where(Message.id == m.id).values(
                            status="no_account",
                            error=(
                                "No unambiguous Telegram account found for this Open Line dialog; "
                                "message was not sent"
                            ),
                        )
                    )
                    await session.commit()
                    logger.warning(
                        "No Telegram account for ChatLink %s tenant=%s connector=%s; refusing to guess",
                        link.id, tenant.id, connector_id or link.provider,
                    )
                    continue

                # Look up Telegram username from most recent inbound message
                tg_recipient = None
                account_username = (tg_account.username or "").lower() if tg_account else ""
                inbound_filters = [
                    Message.chat_link_id == link.id,
                    Message.direction == "inbound",
                    Message.recipient.isnot(None),
                    Message.recipient != "",
                ]
                if account_username:
                    inbound_filters.append(Message.recipient != f"@{account_username}")
                recent_inbound = (await session.execute(
                    select(Message).where(*inbound_filters).order_by(Message.id.desc()).limit(1)
                )).scalars().first()
                if recent_inbound and recent_inbound.recipient and not str(link.external_chat_id).startswith("-"):
                    recipient_candidate = str(recent_inbound.recipient).strip()
                    if recipient_candidate and recipient_candidate != f"@{account_username}":
                        tg_recipient = recipient_candidate

                # Send clean text to Telegram (without operator name prefix)
                tg_text = clean_text or text
                outbox_payload = {
                    "external_chat_id": link.external_chat_id,
                    "text": tg_text,
                    "files": files,
                    "message_db_id": message_db_id,
                    "im": im,
                    "delivery_chat_id": bitrix_chat_id,
                    "connector_id": connector_id or _primary_connector_id(),
                    "line_id": _primary_line_id(link.bitrix_line_id),
                }
                if tg_recipient:
                    outbox_payload["tg_recipient"] = tg_recipient
                if tg_account:
                    outbox_payload["account_id"] = tg_account.id
                    # Include token for file downloads from Bitrix24
                    outbox_payload["domain"] = domain
                    outbox_payload["token_enc"] = tenant.access_token_encrypted
                else:
                    provider_key = f"{link.provider}_{channel.id}" if channel else link.provider
                    outbox_payload["provider_key"] = provider_key
                    outbox_payload["channel_id"] = channel.id if channel else None

                await outbox.enqueue(OutboxMessage(
                    direction="to_external",
                    payload=outbox_payload,
                    max_retries=3,
                ))
                log_message_event("outbound", link.provider,
                                  str(channel.id) if channel else "unknown",
                                  message_id=str(msg_ids), status="queued")
            else:
                error_text = (
                    f"Ambiguous ChatLink for bitrix_chat_id={bitrix_chat_id}; "
                    "Bitrix event did not include internal im.chat_id"
                ) if link_ambiguous else f"No ChatLink found for bitrix_chat_id={bitrix_chat_id}"
                await session.execute(
                    update(Message).where(Message.id == m.id).values(
                        status="no_link",
                        error=error_text,
                    )
                )
                await session.commit()
                logger.warning(f"{error_text} domain={domain}")

    return {"success": True}


@router.post("/api/admin/refresh-token")
async def admin_refresh_token(request: Request):
    """Manually trigger token refresh for all tenants."""
    body = await request.json()
    domain = body.get("domain", "")
    import httpx

    async with async_session() as session:
        if domain:
            result = await session.execute(select(Tenant).where(Tenant.bitrix_domain == domain))
            tenants = [result.scalar_one_or_none()]
            tenants = [t for t in tenants if t]
        else:
            result = await session.execute(select(Tenant))
            tenants = result.scalars().all()

    refreshed = []
    errors = []
    for tenant in tenants:
        try:
            from app.crypto import decrypt_token, encrypt_token
            from sqlalchemy import update as sa_update
            refresh = decrypt_token(tenant.refresh_token_encrypted)
            if not refresh:
                errors.append({"domain": tenant.bitrix_domain, "error": "No refresh token"})
                continue

            async with httpx.AsyncClient() as client:
                r = await client.post(
                    f"https://{tenant.bitrix_domain}/oauth/token/",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": settings.bitrix24_client_id,
                        "client_secret": settings.bitrix24_client_secret,
                        "refresh_token": refresh,
                    },
                    timeout=10,
                )
                data = r.json()

            if "access_token" in data:
                new_token = encrypt_token(data["access_token"])
                new_refresh = encrypt_token(data["refresh_token"]) if "refresh_token" in data else tenant.refresh_token_encrypted
                try:
                    expires_at = utcnow() + timedelta(seconds=int(data.get("expires_in") or 3600))
                except (TypeError, ValueError):
                    expires_at = utcnow() + timedelta(hours=1)
                async with async_session() as session:
                    await session.execute(
                        sa_update(Tenant).where(Tenant.id == tenant.id).values(
                            access_token_encrypted=new_token,
                            refresh_token_encrypted=new_refresh,
                            expires_at=expires_at,
                        )
                    )
                    await session.commit()
                refreshed.append(tenant.bitrix_domain)
                logger.info(f"Manual token refresh OK for {tenant.bitrix_domain}")
            else:
                err = data.get("error_description", data.get("error", "unknown"))
                errors.append({"domain": tenant.bitrix_domain, "error": err})
        except Exception as e:
            errors.append({"domain": tenant.bitrix_domain, "error": str(e)})

    return {"success": True, "refreshed": refreshed, "errors": errors}


@router.post("/api/inbound/{provider}/{channel_id}")
async def inbound_message(provider: str, channel_id: str, request: Request):
    raw_bytes = await _read_limited_body(request, _max_body_bytes())
    secret = settings.inbound_webhook_token or settings.bitrix_webhook_token
    if not _verify_bearer_or_hmac(request, raw_bytes, secret):
        logger.warning("inbound_message: rejected request with invalid token/signature")
        raise HTTPException(status_code=403, detail="Invalid inbound webhook signature")
    try:
        body = json.loads(raw_bytes.decode("utf-8", errors="replace"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    external_chat_id = body.get("external_chat_id", "")
    external_user_id = body.get("external_user_id", "")
    external_user_name = body.get("external_user_name", "")
    text = body.get("text", "")
    attachments = body.get("attachments")
    provider_message_id, idempotency_key, generated_provider_message_id = _inbound_message_identity(
        provider,
        channel_id,
        body,
        external_chat_id,
        external_user_id,
        text,
        attachments,
    )

    async with async_session() as session:
        existing = await session.execute(
            select(Message).where(Message.idempotency_key == idempotency_key)
        )
        if existing.scalar_one_or_none():
            return {"success": True, "status": "duplicate"}

        channel_filters = [Channel.provider == provider]
        try:
            numeric_channel_id = int(channel_id)
        except (TypeError, ValueError):
            numeric_channel_id = None
        if numeric_channel_id:
            channel_filters.append(Channel.id == numeric_channel_id)
        ch_result = await session.execute(select(Channel).where(*channel_filters))
        channel = ch_result.scalars().first()
        if not channel:
            return {"success": False, "message": f"Channel not found for provider {provider} and id {channel_id}"}

        tenant_result = await session.execute(
            select(Tenant).where(Tenant.id == channel.tenant_id)
        )
        tenant = tenant_result.scalar_one_or_none()
        if not tenant:
            return {"success": False, "message": "Tenant not found"}

        link_result = await session.execute(
            select(ChatLink).where(
                ChatLink.tenant_id == tenant.id,
                ChatLink.provider == provider,
                ChatLink.external_chat_id == external_chat_id,
                ChatLink.channel_id == channel.id,
            )
        )
        link = link_result.scalar_one_or_none()
        if not link:
            legacy_link_result = await session.execute(
                select(ChatLink).where(
                    ChatLink.tenant_id == tenant.id,
                    ChatLink.provider == provider,
                    ChatLink.external_chat_id == external_chat_id,
                    ChatLink.channel_id.is_(None),
                )
            )
            link = legacy_link_result.scalar_one_or_none()
        if link and (link.channel_id != channel.id or link.bitrix_line_id != channel.bitrix_line_id):
            await session.execute(
                update(ChatLink).where(ChatLink.id == link.id).values(
                    channel_id=channel.id,
                    bitrix_line_id=channel.bitrix_line_id,
                    updated_at=utcnow(),
                )
            )
            await session.commit()

        if not link:
            link = ChatLink(
                tenant_id=tenant.id,
                channel_id=channel.id,
                provider=provider,
                external_chat_id=external_chat_id,
                external_user_id=external_user_id,
                external_user_name=external_user_name,
                bitrix_line_id=channel.bitrix_line_id,
            )
            session.add(link)
            await session.commit()
            await session.refresh(link)

        msg_payload = {
            "user": {
                "id": external_user_id or external_chat_id,
                "name": external_user_name or "User",
            },
            "message": {
                "id": provider_message_id or str(int(time.time())),
                "date": int(time.time()),
                "text": text,
            },
            "chat": {
                "id": external_chat_id,
                "name": external_user_name or external_chat_id,
            },
        }
        if attachments:
            msg_payload["message"]["files"] = [
                {"url": a.get("url", ""), "name": a.get("name", "file")} for a in attachments
            ]

        start_time = time.time()
        try:
            result = await bx.send_messages(
                tenant.bitrix_domain, tenant.access_token_encrypted,
                _primary_connector_id(), _primary_line_id(channel.bitrix_line_id), [msg_payload],
            )
        except Exception as e:
            logger.warning(f"Bitrix24 send_messages failed: {e}")
            result = None
        duration_ms = (time.time() - start_time) * 1000

        bx_status = bx.send_messages_status(result)
        new_bitrix_chat_id = bx_status.get("chat_id") if bx_status["success"] else None
        new_bitrix_dialog_id = bx_status.get("dialog_id") if bx_status["success"] else None

        log_api_call("imconnector.send.messages", tenant.bitrix_domain,
                     "success" if bx_status["success"] else "failed", duration_ms)

        if new_bitrix_chat_id and not link.bitrix_chat_id:
            await session.execute(
                update(ChatLink).where(ChatLink.id == link.id).values(
                    bitrix_chat_id=str(new_bitrix_chat_id),
                    bitrix_dialog_id=str(new_bitrix_dialog_id) if new_bitrix_dialog_id else None,
                    updated_at=utcnow(),
                )
            )

        status = "failed"
        error = bx_status.get("error")
        if bx_status["success"]:
            if new_bitrix_chat_id or link.bitrix_chat_id:
                status = "sent"
                error = None
            else:
                status = "partial_failed"
                error = "Bitrix24 accepted message but did not return Open Line CHAT_ID"
        raw_payload = dict(body)
        raw_payload["bitrix_send"] = bx_status
        if generated_provider_message_id:
            raw_payload["generated_provider_message_id"] = provider_message_id

        m = Message(
            tenant_id=tenant.id,
            channel_id=channel.id,
            chat_link_id=link.id,
            direction="inbound",
            provider_message_id=provider_message_id,
            text=text,
            status=status,
            error=error,
            raw_payload_json=json.dumps(raw_payload, ensure_ascii=False),
            idempotency_key=idempotency_key,
        )
        session.add(m)
        await session.commit()

    log_message_event("inbound", provider, channel_id,
                      message_id=provider_message_id,
                      status=status)

    return {"success": True, "bitrix_chat_id": new_bitrix_chat_id}


@router.get("/api/open-lines/{domain}")
async def get_open_lines(domain: str):
    async with async_session() as session:
        result = await session.execute(select(Tenant).where(Tenant.bitrix_domain == domain))
        tenant = result.scalar_one_or_none()
        if not tenant:
            return {"success": False, "message": "Tenant not found"}
    try:
        lines = await bx.get_open_lines(domain, tenant.access_token_encrypted)
    except Exception as e:
        logger.warning(f"Failed to get open lines: {e}")
        lines = None
    return {"success": True, "lines": lines}


@router.get("/api/channels/{domain}")
async def get_channels(domain: str):
    async with async_session() as session:
        result = await session.execute(select(Tenant).where(Tenant.bitrix_domain == domain))
        tenant = result.scalar_one_or_none()
        if not tenant:
            return {"success": True, "channels": []}
        ch_result = await session.execute(
            select(Channel).where(Channel.tenant_id == tenant.id)
        )
        channels = ch_result.scalars().all()
        return {
            "success": True,
            "channels": [
                {"id": c.id, "provider": c.provider, "name": c.name,
                 "bitrix_line_id": c.bitrix_line_id, "status": c.status}
                for c in channels
            ],
        }


@router.get("/api/messages/{domain}")
async def get_messages(domain: str):
    async with async_session() as session:
        result = await session.execute(select(Tenant).where(Tenant.bitrix_domain == domain))
        tenant = result.scalar_one_or_none()
        if not tenant:
            return {"success": True, "messages": []}
        msg_result = await session.execute(
            select(Message).where(Message.tenant_id == tenant.id)
            .order_by(Message.created_at.desc()).limit(100)
        )
        messages = msg_result.scalars().all()
        return {
            "success": True,
            "messages": [
                {
                    "id": m.id,
                    "direction": m.direction,
                    "text": m.text,
                    "status": m.status,
                    "error": m.error,
                    "provider_message_id": m.provider_message_id,
                    "bitrix_message_id": m.bitrix_message_id,
                    "sender_name": m.sender_name,
                    "recipient": m.recipient,
                    "created_at": str(m.created_at) if m.created_at else None,
                }
                for m in messages
            ],
        }


@router.get("/api/outbox/stats")
async def outbox_stats():
    return {"success": True, "stats": await outbox.get_stats()}


@router.post("/api/outbox/retry/{entry_id}")
async def retry_outbox_entry(entry_id: int):
    success = await outbox.retry_entry(entry_id)
    return {"success": success, "message": "Retrying" if success else "Entry not found or not in failed state"}


@router.get("/api/outbox/failed")
async def list_failed_outbox():
    async with async_session() as session:
        result = await session.execute(
            select(OutboxEntry).where(OutboxEntry.status == "failed")
            .order_by(OutboxEntry.created_at.desc()).limit(50)
        )
        entries = result.scalars().all()
        return {
            "success": True,
            "entries": [
                {
                    "id": e.id,
                    "direction": e.direction,
                    "attempts": e.attempts,
                    "last_error": e.last_error,
                    "created_at": str(e.created_at),
                    "payload": json.loads(e.payload_json) if e.payload_json else {},
                }
                for e in entries
            ],
        }


@router.api_route("/bitrix/widget", methods=["GET", "POST"])
async def bitrix_widget(request: Request):
    import os
    query = dict(request.query_params)
    body = {}
    if request.method == "POST":
        raw = await _read_limited_body(request, _max_body_bytes())
        raw_str = raw.decode("utf-8", errors="replace")
        if "application/json" in request.headers.get("content-type", ""):
            try:
                body = json.loads(raw_str)
            except Exception:
                body = {}
        else:
            try:
                body = _parse_php_form(raw_str)
            except Exception:
                body = {}

    placement = body.get("PLACEMENT") or query.get("PLACEMENT", "")
    placement_options = body.get("PLACEMENT_OPTIONS") or query.get("PLACEMENT_OPTIONS", "{}")
    domain = _bitrix_domain_from_request(request, body)
    options = _safe_json_object(placement_options)
    entity_id = options.get("ID", "")
    entity_type = "deal" if "deal" in placement.lower() else "contact" if "contact" in placement.lower() else ""
    widget_token = ""
    if await _valid_bitrix_widget_bootstrap(request, domain, body):
        widget_token = _make_widget_token(domain)
    else:
        logger.info("bitrix_widget: deferring API token bootstrap to browser for domain=%r", domain)

    template_path = os.path.join(os.path.dirname(__file__), "_widget_template.html")
    with open(template_path, "r") as f:
        html = f.read()

    html = html.replace("%%DOMAIN_JSON%%", _js_literal(domain))
    html = html.replace("%%APP_URL_JSON%%", _js_literal(settings.app_url))
    html = html.replace("%%ENTITY_TYPE_JSON%%", _js_literal(entity_type))
    html = html.replace("%%ENTITY_ID_JSON%%", _js_literal(entity_id))
    html = html.replace("%%WIDGET_TOKEN_JSON%%", _js_literal(widget_token))

    return HTMLResponse(content=html, headers={
        "X-Frame-Options": "ALLOWALL",
        "Content-Security-Policy": "frame-ancestors *",
    })


@router.post("/api/widget/bootstrap")
async def widget_bootstrap(request: Request):
    raw = await _read_limited_body(request, min(_max_body_bytes(widget=True), 64 * 1024))
    raw_str = raw.decode("utf-8", errors="replace")
    if "application/json" in request.headers.get("content-type", ""):
        try:
            body = json.loads(raw_str) if raw_str else {}
        except Exception:
            body = {}
    else:
        try:
            body = _parse_php_form(raw_str) if raw_str else {}
        except Exception:
            body = {}
    if not isinstance(body, dict):
        body = {}

    domain = _bitrix_domain_from_request(request, body)
    if not domain:
        raise HTTPException(status_code=400, detail="Missing Bitrix domain")
    if not await _valid_bitrix_widget_bootstrap(request, domain, body):
        raise HTTPException(status_code=403, detail="Invalid Bitrix bootstrap")
    return {
        "success": True,
        "domain": domain,
        "widget_token": _make_widget_token(domain),
    }


@router.get("/api/widget/accounts")
async def widget_accounts(request: Request):
    query = dict(request.query_params)
    domain = query.get("DOMAIN", "")
    if not domain:
        return JSONResponse(content=[])
    _require_widget_auth(request, domain)
    try:
        async with async_session() as session:
            result = await session.execute(select(Tenant).where(Tenant.bitrix_domain == domain))
            tenant = result.scalar_one_or_none()
            if not tenant:
                return JSONResponse(content=[])
            acc_result = await session.execute(
                select(TelegramAccount).where(
                    TelegramAccount.tenant_id == tenant.id,
                    TelegramAccount.state.in_(["active", "authorized"]),
                    TelegramAccount.bitrix_line_id == _primary_line_id(),
                )
            )
            accounts = acc_result.scalars().all()
            return JSONResponse(content=[
                {"id": a.id, "username": a.username, "phone": a.phone,
                 "line_id": a.bitrix_line_id, "state": a.state}
                for a in accounts
            ])
    except Exception as e:
        logger.error(f"widget_accounts error: {e}", exc_info=True)
        return JSONResponse(content=[], status_code=500)


@router.get("/api/widget/quick-replies")
async def widget_quick_replies(request: Request):
    query = dict(request.query_params)
    domain = query.get("DOMAIN", "")
    if not domain:
        return JSONResponse(content=[])
    _require_widget_auth(request, domain)
    try:
        async with async_session() as session:
            tenant = (await session.execute(select(Tenant).where(Tenant.bitrix_domain == domain))).scalar_one_or_none()
            if not tenant:
                return JSONResponse(content=[])
            rows = (await session.execute(
                select(QuickReplyTemplate)
                .where(
                    QuickReplyTemplate.tenant_id == tenant.id,
                    QuickReplyTemplate.active == 1,
                )
                .order_by(QuickReplyTemplate.sort_order.asc(), QuickReplyTemplate.id.asc())
                .limit(35)
            )).scalars().all()
        return JSONResponse(content=[
            {"id": item.id, "title": item.title, "text": item.text}
            for item in rows
        ])
    except Exception as e:
        logger.error(f"widget_quick_replies error: {e}", exc_info=True)
        return JSONResponse(content=[], status_code=500)


@router.post("/api/widget/send")
async def widget_send(request: Request):
    raw_body = await _read_limited_body(request, _max_body_bytes(widget=True))
    try:
        body = json.loads(raw_body.decode("utf-8", errors="replace"))
    except Exception:
        return JSONResponse(content={"success": False, "error": "invalid JSON body"}, status_code=400)
    domain = _widget_domain_from_request(request, body)
    if not domain:
        return JSONResponse(content={"success": False, "error": "domain required"}, status_code=400)
    _require_widget_auth(request, domain)
    try:
        account_id = int(body.get("account_id") or 0)
    except (TypeError, ValueError):
        account_id = 0
    try:
        chat_link_id = int(body.get("chat_link_id") or 0) or None
    except (TypeError, ValueError):
        chat_link_id = None
    raw_recipient = body.get("recipient", "")
    recipient = _normalize_widget_recipient(raw_recipient)
    text = body.get("text", "").strip()
    files = _decode_widget_files(body.get("files", []))
    create_ol = _as_bool(body.get("create_ol_chat"), False)
    create_lead = _as_bool(body.get("create_lead"), settings.create_lead_by_default)
    add_timeline = _as_bool(body.get("add_to_timeline"), False)
    entity_type = body.get("entity_type", "")
    entity_id = body.get("entity_id", "")
    operator_id = str(body.get("operator_id") or "").strip()
    operator_name = str(body.get("operator_name") or "").strip()
    if operator_id and not operator_name:
        operator_name = f"Bitrix user #{operator_id}"
    client_request_id = str(body.get("client_request_id") or body.get("request_id") or "").strip()[:160]
    payload_fingerprint = _widget_payload_fingerprint(
        account_id,
        recipient,
        text,
        files,
        create_ol,
        create_lead,
        add_timeline,
        entity_type,
        entity_id,
    )

    if not account_id or not recipient or not (text or files):
        return JSONResponse(
            content={"success": False, "error": "account_id, recipient and text or files required"},
            status_code=400,
        )

    try:
        # Load account and extract scalar values while session is open
        connector_id = _primary_connector_id()
        line_id = _primary_line_id()
        acc_session_string = None
        acc_api_id = None
        acc_api_hash = None
        acc_id = None
        tenant_domain = None
        tenant_token_enc = None
        tenant_id_db = None
        request_key = ""
        existing_widget_message_id = None
        widget_link_id = chat_link_id
        widget_link_external_chat_id = ""

        async with async_session() as session:
            tenant_result = await session.execute(select(Tenant).where(Tenant.bitrix_domain == domain))
            tenant = tenant_result.scalar_one_or_none()
            if not tenant:
                return JSONResponse(content={"success": False, "error": "Tenant not found"}, status_code=404)
            tenant_domain = tenant.bitrix_domain
            tenant_token_enc = tenant.access_token_encrypted
            tenant_id_db = tenant.id

            request_key = f"widget_{tenant_id_db}_{account_id}_{client_request_id}" if client_request_id else ""
            if request_key:
                existing = (await session.execute(
                    select(Message).where(Message.idempotency_key == request_key)
                )).scalar_one_or_none()
                if existing:
                    existing_raw = _raw_payload_dict(existing.raw_payload_json)
                    existing_fingerprint = existing_raw.get("payload_fingerprint")
                    if existing_fingerprint and existing_fingerprint != payload_fingerprint:
                        return JSONResponse(
                            content={
                                "success": False,
                                "error": "client_request_id was already used for a different message",
                                "message_id": existing.id,
                            },
                            status_code=409,
                        )
                    if existing.status in {"sent", "queued", "sending", "partial_failed"}:
                        is_partial = existing.status == "partial_failed"
                        return JSONResponse(
                            content={
                                "success": not is_partial,
                                "partial_success": is_partial,
                                "telegram_sent": is_partial,
                                "duplicate": True,
                                "message_id": existing.id,
                                "error": existing.error if is_partial else None,
                            },
                            status_code=207 if is_partial else 200,
                        )
                    existing_widget_message_id = existing.id

            result = await session.execute(
                select(TelegramAccount).where(
                    TelegramAccount.id == account_id,
                    TelegramAccount.tenant_id == tenant_id_db,
                    TelegramAccount.state.in_(["active", "authorized"]),
                )
            )
            account = result.scalar_one_or_none()
            if not account:
                return JSONResponse(content={"success": False, "error": "Account not found for this tenant"}, status_code=404)
            if widget_link_id:
                widget_link = await session.get(ChatLink, widget_link_id)
                if not widget_link or widget_link.tenant_id != tenant_id_db:
                    return JSONResponse(content={"success": False, "error": "Chat link not found"}, status_code=404)
                if widget_link.account_id and int(widget_link.account_id) != int(account_id):
                    return JSONResponse(
                        content={"success": False, "error": "Selected account does not own this chat"},
                        status_code=409,
                    )
                if widget_link.provider and widget_link.provider != _primary_connector_id(account.bitrix_connector_id):
                    return JSONResponse(
                        content={"success": False, "error": "Selected account connector does not match this chat"},
                        status_code=409,
                    )
                widget_link_external_chat_id = widget_link.external_chat_id or ""
            # Extract values while session is open (prevent DetachedInstanceError)
            connector_id = _primary_connector_id(account.bitrix_connector_id)
            line_id = _primary_line_id(account.bitrix_line_id)
            acc_session_string = decrypt_if_encrypted(account.session_string)
            acc_api_id = account.api_id
            acc_api_hash = decrypt_if_encrypted(account.api_hash)
            acc_id = account.id

        from app.telegram.service import send_by_entity, send_file, _active_clients

        client = _active_clients.get(acc_id)
        if not client or not client.is_connected():
            from app.telegram.service import start_listener_task
            from app.api.telegram_routes import _telegram_inbound_callback as _tg_cb
            if acc_session_string:
                start_listener_task(acc_id, acc_session_string, acc_api_id, acc_api_hash, _tg_cb)
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    client = _active_clients.get(acc_id)
                    if client and client.is_connected():
                        break
                    await asyncio.sleep(0.25)
                else:
                    return JSONResponse(
                        content={"success": False, "error": "Telegram client not ready"},
                        status_code=503,
                    )
            else:
                return JSONResponse(content={"success": False, "error": "No session"}, status_code=400)

        if widget_link_external_chat_id:
            try:
                target = int(widget_link_external_chat_id)
            except (TypeError, ValueError):
                target = widget_link_external_chat_id
        else:
            target = recipient if recipient.startswith(("@", "+")) else f"@{recipient}"
        tg_user_id = str(widget_link_external_chat_id) if widget_link_external_chat_id else None
        client = _active_clients.get(acc_id)
        if client and not widget_link_external_chat_id:
            try:
                entity = await client.get_entity(target)
                tg_user_id = getattr(entity, "id", None)
            except Exception as e:
                logger.info(f"widget_send: could not resolve Telegram entity before send: {e}")

        log_entry = []
        partial_failures = []
        link_id = None
        channel_id = None
        bitrix_files = []
        if files:
            for file_info in files:
                saved_filename = await _save_media(file_info["bytes"], file_info["name"])
                public_url = signed_media_url(saved_filename)
                bitrix_files.append(
                    _build_bitrix_file_entry(public_url, file_info["name"], len(file_info["bytes"]))
                )

        raw_log = {
            "source": "widget",
            "recipient": recipient,
            "raw_recipient": raw_recipient,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "create_ol_chat": create_ol,
            "create_lead": create_lead,
            "operator_id": operator_id,
            "operator_name": operator_name,
            "chat_link_id": widget_link_id,
            "client_request_id": client_request_id,
            "payload_fingerprint": payload_fingerprint,
            "files": [
                {
                    "name": f["name"],
                    "size": len(f["bytes"]),
                    "content_type": f["content_type"],
                    "type": _file_type_from_name(f["name"], f["content_type"]),
                    "url": bitrix_files[idx]["url"] if idx < len(bitrix_files) else "",
                }
                for idx, f in enumerate(files)
            ],
        }
        display_text = text or _files_label(raw_log["files"])
        message_db_id = None
        widget_idempotency_key = request_key or f"widget_{tenant_id_db}_{account_id}_{recipient}_{time.time_ns()}"
        async with async_session() as session:
            if existing_widget_message_id:
                await session.execute(
                    update(Message).where(Message.id == existing_widget_message_id).values(
                        text=display_text,
                        status="sending",
                        error=None,
                        raw_payload_json=json.dumps(raw_log, ensure_ascii=False),
                        account_id=account_id,
                        chat_id_tg=str(tg_user_id) if tg_user_id else None,
                        sender_name=operator_name or "Bitrix widget",
                        recipient=str(target),
                        chat_link_id=widget_link_id,
                        updated_at=utcnow(),
                    )
                )
                await session.commit()
                message_db_id = existing_widget_message_id
            else:
                m = Message(
                    tenant_id=tenant_id_db,
                    channel_id=None,
                    chat_link_id=widget_link_id,
                    direction="outbound",
                    text=display_text,
                    status="sending",
                    raw_payload_json=json.dumps(raw_log, ensure_ascii=False),
                    idempotency_key=widget_idempotency_key,
                    account_id=account_id,
                    chat_id_tg=str(tg_user_id) if tg_user_id else None,
                    sender_name=operator_name or "Bitrix widget",
                    recipient=str(target),
                )
                session.add(m)
                await session.commit()
                await session.refresh(m)
                message_db_id = m.id

        file_send_results = []
        telegram_message_ids = []
        if files:
            for idx, file_info in enumerate(files):
                result = await send_file(
                    acc_id,
                    target,
                    file_info["bytes"],
                    file_info["name"],
                    caption=text if idx == 0 else "",
                )
                if result.get("user_id"):
                    tg_user_id = result.get("user_id")
                telegram_message_ids.extend(_message_ids_from_result(result))
                file_send_results.append(result)

        file_errors = [
            f"{files[idx]['name']}: {result.get('error', 'send failed')}"
            for idx, result in enumerate(file_send_results)
            if idx < len(files) and not result.get("success")
        ]
        tg_result = {"success": any(r.get("success") for r in file_send_results), "partial_errors": file_errors} if files else None
        if text and not files:
            tg_result = await send_by_entity(acc_id, target, text)
            tg_user_id = tg_result.get("user_id") or tg_user_id
            telegram_message_ids.extend(_message_ids_from_result(tg_result))
        elif text and files and not any(r.get("success") for r in file_send_results):
            tg_result = await send_by_entity(acc_id, target, text)
            tg_user_id = tg_result.get("user_id") or tg_user_id
            telegram_message_ids.extend(_message_ids_from_result(tg_result))
        if tg_result is not None and telegram_message_ids:
            tg_result["message_ids"] = telegram_message_ids
            tg_result["message_id"] = telegram_message_ids[0]

        if not tg_user_id and client:
            try:
                entity = await client.get_entity(target)
                tg_user_id = getattr(entity, "id", None)
            except Exception:
                pass

        if not tg_result or not tg_result.get("success"):
            async with async_session() as session:
                await session.execute(
                    update(Message).where(Message.id == message_db_id).values(
                        status="failed",
                        error="Failed to send via Telegram",
                        updated_at=utcnow(),
                    )
                )
                await session.commit()
            return JSONResponse(
                content={"success": False, "error": "Failed to send via Telegram"}, status_code=500
            )
        tg_user_id = tg_user_id or tg_result.get("user_id")
        if file_errors:
            raw_log["partial_errors"] = file_errors
            partial_failures.extend(file_errors)
            log_entry.append("partial_file_errors: " + "; ".join(file_errors[:3]))
        if telegram_message_ids:
            raw_log["telegram_message_ids"] = telegram_message_ids
        logger.info(f"widget_send: tg_sent ok, user_id={tg_user_id} connector={connector_id} line={line_id} domain={domain!r}")

        logger.info(f"widget_send: tenant_id_db={tenant_id_db} tenant_domain={tenant_domain!r}")

        if tenant_id_db and (tg_user_id or widget_link_id):
            async with async_session() as session:
                channel_result = await session.execute(
                    select(Channel).where(
                        Channel.tenant_id == tenant_id_db,
                        Channel.provider == connector_id,
                        Channel.bitrix_line_id == line_id,
                    )
                )
                channel = channel_result.scalars().first()
                channel_id = channel.id if channel else None

                link = await session.get(ChatLink, widget_link_id) if widget_link_id else None
                if not link and tg_user_id:
                    link_result = await session.execute(
                        select(ChatLink).where(
                            ChatLink.tenant_id == tenant_id_db,
                            ChatLink.provider == connector_id,
                            ChatLink.external_chat_id == str(tg_user_id),
                            ChatLink.account_id == account_id,
                        ).order_by(ChatLink.bitrix_chat_id.is_(None), ChatLink.id.desc())
                    )
                    link = link_result.scalars().first()
                if not link and tg_user_id:
                    legacy_result = await session.execute(
                        select(ChatLink).where(
                            ChatLink.tenant_id == tenant_id_db,
                            ChatLink.provider == connector_id,
                            ChatLink.external_chat_id == str(tg_user_id),
                            ChatLink.account_id.is_(None),
                        ).order_by(ChatLink.bitrix_chat_id.is_(None), ChatLink.id.desc())
                    )
                    link = legacy_result.scalars().first()
                if link:
                    link_id = link.id
                    if (
                        link.bitrix_line_id != line_id
                        or link.channel_id != channel_id
                        or link.account_id != account_id
                    ):
                        link.bitrix_line_id = line_id
                        link.channel_id = channel_id
                        link.account_id = account_id
                        await session.commit()
                else:
                    link = ChatLink(
                        tenant_id=tenant_id_db,
                        channel_id=channel_id,
                        external_chat_id=str(tg_user_id),
                        provider=connector_id,
                        bitrix_line_id=line_id,
                        account_id=account_id,
                    )
                    session.add(link)
                    await session.commit()
                    await session.refresh(link)
                    link_id = link.id

        async with async_session() as session:
            await session.execute(
                update(Message).where(Message.id == message_db_id).values(
                    channel_id=channel_id,
                    chat_link_id=link_id,
                    status="sent",
                    provider_message_id=",".join(telegram_message_ids) if telegram_message_ids else None,
                    raw_payload_json=json.dumps(raw_log, ensure_ascii=False),
                    chat_id_tg=str(tg_user_id) if tg_user_id else None,
                    updated_at=utcnow(),
                )
            )
            await session.commit()

        # Create or continue Open Lines session in Bitrix24 only when requested.
        bitrix_external_chat_id = str(tg_user_id or widget_link_external_chat_id or "")
        if create_ol and bitrix_external_chat_id and tenant_id_db and connector_id and line_id:
            try:
                user_display = recipient if recipient.startswith("@") else f"@{recipient}"
                existing_chat_id = None

                async with async_session() as session:
                    link = await session.get(ChatLink, widget_link_id) if widget_link_id else None
                    if not link:
                        link_result = await session.execute(
                            select(ChatLink).where(
                                ChatLink.tenant_id == tenant_id_db,
                                ChatLink.provider == connector_id,
                                ChatLink.external_chat_id == bitrix_external_chat_id,
                                ChatLink.account_id == account_id,
                            ).order_by(ChatLink.bitrix_chat_id.is_(None), ChatLink.id.desc())
                        )
                        link = link_result.scalars().first()
                    if not link:
                        legacy_result = await session.execute(
                            select(ChatLink).where(
                                ChatLink.tenant_id == tenant_id_db,
                                ChatLink.provider == connector_id,
                                ChatLink.external_chat_id == bitrix_external_chat_id,
                                ChatLink.account_id.is_(None),
                            ).order_by(ChatLink.bitrix_chat_id.is_(None), ChatLink.id.desc())
                        )
                        link = legacy_result.scalars().first()
                    if link:
                        existing_chat_id = link.bitrix_chat_id
                        link_id = link.id
                        if link.account_id != account_id:
                            link.account_id = account_id
                            link.bitrix_line_id = line_id
                            await session.commit()

                ol_message_id = telegram_message_ids[0] if telegram_message_ids else (request_key or f"widget_message_{message_db_id}")
                message_block = {
                    "id": ol_message_id,
                    "date": int(time.time()),
                    "text": text,
                    "disable_crm": "Y",
                }
                if bitrix_files:
                    message_block["files"] = bitrix_files

                ol_result = await bx.send_messages(
                    tenant_domain,
                    tenant_token_enc,
                    connector_id,
                    line_id,
                    [{
                        "user": _bitrix_external_user_from_recipient(recipient, tg_user_id),
                        "message": message_block,
                        "chat": {"id": bitrix_external_chat_id, "name": user_display},
                    }]
                )
                logger.info(f"widget_send OL result: {ol_result}")
                ol_status = bx.send_messages_status(ol_result)
                if not ol_status["success"]:
                    partial_failures.append(f"Open Lines error: {ol_status.get('error') or 'Bitrix24 did not confirm delivery'}")
                    log_entry.append(f"ol_error: {ol_status.get('error') or 'Bitrix24 did not confirm delivery'}")
                if existing_chat_id:
                    log_entry.append(f"continued_chat: {existing_chat_id}")
                new_chat_id = ol_status.get("chat_id") or ""
                new_dialog_id = ol_status.get("dialog_id") or ""
                if not existing_chat_id and not new_chat_id:
                    partial_failures.append("Open Lines error: Bitrix24 did not return session CHAT_ID")
                    log_entry.append("ol_error: missing session CHAT_ID")
                if new_chat_id and link_id:
                    async with async_session() as session:
                        link_row = await session.get(ChatLink, link_id)
                        if link_row:
                            link_row.bitrix_chat_id = str(new_chat_id)
                            link_row.bitrix_dialog_id = str(new_dialog_id) if new_dialog_id else link_row.bitrix_dialog_id
                            link_row.last_message_at = utcnow()
                            await session.commit()
                    log_entry.append(f"ol_session_chat_id: {new_chat_id}")
                log_entry.append(f"ol_result: {ol_result}")
                log_entry.append("manual_lead_enabled" if create_lead else "manual_lead_disabled")
            except Exception as e:
                logger.warning(f"widget_send: Could not create Open Lines session: {e}", exc_info=True)
                partial_failures.append(f"Open Lines error: {e}")
                log_entry.append(f"ol_error: {e}")
        elif create_ol:
            partial_failures.append("Open Lines skipped: missing Telegram user or Bitrix line")
            log_entry.append("ol_error: missing Telegram user or Bitrix line")
        elif not create_ol:
            log_entry.append("ol_skipped")

        if create_lead and link_id and tenant_domain and tenant_token_enc:
            try:
                lead_result = await _ensure_lead_for_link(
                    tenant_domain,
                    tenant_token_enc,
                    link_id,
                    connector_id,
                    display_name=recipient,
                    recipient=recipient,
                    first_text=text or _files_label(bitrix_files),
                    assigned_by_id=operator_id,
                )
                log_entry.append(f"lead: {lead_result}")
                lead_error = ""
                if isinstance(lead_result, dict):
                    lead_error = lead_result.get("error") or lead_result.get("reason") or ""
                if not (isinstance(lead_result, dict) and lead_result.get("success")):
                    partial_failures.append(
                        f"Lead error: {lead_error or 'Bitrix24 did not create lead'}"
                    )
            except Exception as e:
                logger.warning(f"widget_send: Could not create lead: {e}", exc_info=True)
                partial_failures.append(f"Lead error: {e}")
                log_entry.append(f"lead_error: {e}")
        elif create_lead:
            partial_failures.append("Lead skipped: missing chat link or Bitrix token")
            log_entry.append("lead_error: missing chat link or Bitrix token")

        token = decrypt_token(tenant_token_enc) if tenant_token_enc else None
        if add_timeline and token and entity_type and entity_id:
            try:
                tl_result = await bx._call(tenant_domain or domain, token, "crm.timeline.comment.add", {
                    "fields": {
                        "ENTITY_ID": int(entity_id),
                        "ENTITY_TYPE": entity_type.lower(),
                        "COMMENT": f"[SAFY Telegram] To: {recipient}\n{text or '[file]'}",
                    }
                })
                log_entry.append(f"timeline: {tl_result}")
                if not tl_result:
                    partial_failures.append("Timeline error: Bitrix24 did not create timeline comment")
            except Exception as e:
                partial_failures.append(f"Timeline error: {e}")
                log_entry.append(f"timeline_error: {e}")
        elif add_timeline:
            partial_failures.append("Timeline skipped: missing Bitrix token or CRM entity context")
            log_entry.append("timeline_error: missing Bitrix token or CRM entity context")

        if partial_failures:
            error_text = "; ".join(str(item) for item in partial_failures[:5])
            raw_log["partial_failures"] = partial_failures
            async with async_session() as session:
                await session.execute(
                    update(Message).where(Message.id == message_db_id).values(
                        status="partial_failed",
                        error=error_text,
                        raw_payload_json=json.dumps(raw_log, ensure_ascii=False),
                        updated_at=utcnow(),
                    )
                )
                await session.commit()
            return JSONResponse(
                content={
                    "success": False,
                    "partial_success": True,
                    "telegram_sent": True,
                    "message_id": message_db_id,
                    "error": f"Sent to Telegram, but follow-up action failed: {error_text}",
                    "log": log_entry,
                },
                status_code=207,
            )

        return JSONResponse(content={"success": True, "message_id": message_db_id, "log": log_entry})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)}, status_code=500)


@router.get("/api/widget/history")
async def widget_history(request: Request):
    query = dict(request.query_params)
    recipient = query.get("recipient", "")
    domain = query.get("DOMAIN", "")
    if not domain:
        return JSONResponse(content=[], status_code=400)
    _require_widget_auth(request, domain)
    try:
        limit = int(query.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    limit = min(max(limit, 1), 200)
    if not recipient:
        return JSONResponse(content=[])
    account_id = None
    chat_link_id = None
    try:
        account_id = int(query.get("account_id") or 0) or None
    except (TypeError, ValueError):
        account_id = None
    try:
        chat_link_id = int(query.get("chat_link_id") or 0) or None
    except (TypeError, ValueError):
        chat_link_id = None
    try:
        async with async_session() as session:
            filters = [Message.chat_link_id == chat_link_id] if chat_link_id else [Message.recipient == recipient]
            if domain:
                tenant_result = await session.execute(select(Tenant).where(Tenant.bitrix_domain == domain))
                tenant = tenant_result.scalar_one_or_none()
                if not tenant:
                    return JSONResponse(content=[])
                filters.append(Message.tenant_id == tenant.id)
            if not chat_link_id and account_id:
                filters.append(Message.account_id == account_id)
            result = await session.execute(
                select(Message).where(*filters).order_by(Message.created_at.desc()).limit(limit)
            )
            messages = result.scalars().all()
            rows = []
            for m in reversed(messages):
                files = _extract_files_from_raw(m.raw_payload_json)
                text_value = m.text or _files_label(files)
                if files and (not text_value or text_value.strip().lower() in {"[file]", "[files]"}):
                    text_value = _files_label(files)
                rows.append({
                    "id": m.id,
                    "direction": m.direction,
                    "status": m.status,
                    "text": text_value,
                    "from_name": m.sender_name or "",
                    "timestamp": str(m.created_at) if m.created_at else "",
                    "files": files,
                })
            return JSONResponse(content=rows)
    except Exception as e:
        logger.error(f"widget_history error: {e}", exc_info=True)
        return JSONResponse(content=[], status_code=500)


@router.get("/api/widget/chats")
async def widget_chats(request: Request):
    query = dict(request.query_params)
    domain = query.get("DOMAIN", "")
    if not domain:
        return JSONResponse(content=[], status_code=400)
    _require_widget_auth(request, domain)
    try:
        async with async_session() as session:
            if domain:
                tenant_result = await session.execute(
                    select(Tenant).where(Tenant.bitrix_domain == domain)
                )
                tenant = tenant_result.scalar_one_or_none()
                if not tenant:
                    return JSONResponse(content=[])
                msg_result = await session.execute(
                    select(Message).where(
                        Message.tenant_id == tenant.id,
                        Message.recipient.isnot(None),
                        Message.recipient != "",
                    ).order_by(Message.created_at.desc()).limit(200)
                )
            else:
                msg_result = await session.execute(
                    select(Message).where(
                        Message.recipient.isnot(None),
                        Message.recipient != "",
                    ).order_by(Message.created_at.desc()).limit(200)
                )
            all_msgs = msg_result.scalars().all()
            link_ids = [m.chat_link_id for m in all_msgs if m.chat_link_id]
            link_map = {}
            if link_ids:
                link_rows = (await session.execute(select(ChatLink).where(ChatLink.id.in_(link_ids)))).scalars().all()
                link_map = {link.id: link for link in link_rows}

        seen = {}
        for m in all_msgs:
            key = m.chat_link_id or (m.recipient, m.account_id or 0)
            if key not in seen:
                seen[key] = m

        chats = [
            {
                "chat_key": f"link:{m.chat_link_id}" if m.chat_link_id else f"{m.account_id or 0}:{m.recipient}",
                "recipient": m.recipient,
                "last_text": m.text,
                "last_from": m.sender_name or "",
                "direction": m.direction,
                "last_time": str(m.created_at) if m.created_at else "",
                "account_id": m.account_id,
                "chat_link_id": m.chat_link_id,
                "bitrix_chat_id": (link_map.get(m.chat_link_id).bitrix_chat_id if link_map.get(m.chat_link_id) else ""),
            }
            for m in seen.values()
        ]
        return JSONResponse(content=chats)
    except Exception as e:
        logger.error(f"widget_chats error: {e}", exc_info=True)
        return JSONResponse(content=[], status_code=500)


@router.get("/api/widget/typing")
async def widget_typing(request: Request):
    query = dict(request.query_params)
    domain = query.get("DOMAIN", "")
    if not domain:
        return JSONResponse(content={"typing": False, "chat_id": ""}, status_code=400)
    _require_widget_auth(request, domain)
    recipient = query.get("recipient", "")
    chat_id = str(query.get("bitrix_chat_id") or "").strip()
    try:
        account_id = int(query.get("account_id") or 0) or None
    except (TypeError, ValueError):
        account_id = None
    if not chat_id and recipient:
        try:
            async with async_session() as session:
                filters = [Message.recipient == recipient, Message.chat_link_id.isnot(None)]
                if domain:
                    tenant_result = await session.execute(select(Tenant).where(Tenant.bitrix_domain == domain))
                    tenant = tenant_result.scalar_one_or_none()
                    if tenant:
                        filters.append(Message.tenant_id == tenant.id)
                if account_id:
                    filters.append(Message.account_id == account_id)
                row = (await session.execute(
                    select(ChatLink)
                    .join(Message, Message.chat_link_id == ChatLink.id)
                    .where(*filters)
                    .order_by(Message.created_at.desc())
                    .limit(1)
                )).scalar_one_or_none()
                chat_id = row.bitrix_chat_id if row else ""
        except Exception as e:
            logger.warning(f"widget_typing: failed to resolve chat_id for recipient {recipient!r}: {e}", exc_info=True)
            chat_id = ""
    status = _TYPING_STATUSES.get((domain or "", chat_id or ""))
    now = time.time()
    if status and status.get("expires_at", 0) >= now and status.get("typing"):
        return JSONResponse(content={"typing": True, "chat_id": chat_id, "updated_at": status.get("updated_at")})
    return JSONResponse(content={"typing": False, "chat_id": chat_id})


@router.get("/health")
async def health():
    return {"status": "ok"}
