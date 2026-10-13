import json
import logging
import os
import html
import csv
import io
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, FileResponse, Response
from sqlalchemy import select, func, or_, and_, update as sa_update, delete as sa_delete, literal_column
from app.database import async_session, Tenant, Connector, Channel, ChatLink, Message, OutboxEntry, TelegramAccount, AdminAuditLog, QuickReplyTemplate, utcnow, DATABASE_URL
from app.config import settings
from app.bitrix import client as bx
from app.bitrix.crm import (
    create_native_openline_lead,
    create_or_get_lead,
    ensure_openline_timeline_note,
    lead_url,
    openline_chat_url,
)
from app.maintenance import BACKUP_DIR, create_backup_async, list_backups, cleanup_backups, send_health_notification

logger = logging.getLogger(__name__)
router = APIRouter()

EXPECTED_BITRIX_EVENTS = (
    "ONIMCONNECTORMESSAGEADD",
    "ONIMCONNECTORMESSAGEUPDATE",
    "ONIMCONNECTORSTATUSDELETE",
    "ONIMCONNECTORLINEDELETE",
)
OPTIONAL_BITRIX_EVENTS = ("OnIMLiveChatStatus",)
SENSITIVE_KEYS = {"access_token", "refresh_token", "token_enc", "api_hash", "session_string", "password", "secret"}
MSK = timezone(timedelta(hours=3))

def _csv_values(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


REAL_OPERATOR_NAME = settings.real_operator_name
UNKNOWN_BITRIX_OPERATOR_NAME = settings.unknown_bitrix_operator_name
SERVICE_OPERATOR_NAMES = set(_csv_values(settings.service_operator_names))
FALLBACK_OPERATOR_NAMES = set(_csv_values(settings.fallback_operator_names))
TEST_TEXT_PREFIXES = tuple(item.lower() for item in _csv_values(settings.test_text_prefixes))


def _build_admin_html(connector_name, connector_id):
    template_path = os.path.join(os.path.dirname(__file__), "_admin_template.html")
    with open(template_path, encoding="utf-8") as f:
        tpl = f.read()
    return (
        tpl
        .replace("{{CONNECTOR_NAME}}", html.escape(str(connector_name or "")))
        .replace("{{CONNECTOR_ID}}", html.escape(str(connector_id or "")))
    )


def _dt(value):
    return value.isoformat() if value else None


def _naive_utc(value):
    if not value:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _parse_date_bound(value: str, end: bool = False):
    if not value:
        return None
    value = value.strip()
    try:
        if len(value) == 10:
            local_dt = datetime.fromisoformat(value).replace(tzinfo=MSK)
            if end:
                local_dt = local_dt + timedelta(days=1)
        else:
            local_dt = datetime.fromisoformat(value)
            if local_dt.tzinfo is None:
                local_dt = local_dt.replace(tzinfo=MSK)
        # SQLite stores these DateTime values without tzinfo, in UTC.
        return local_dt.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


def _local_day(value) -> str:
    if not value:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(MSK).date().isoformat()


def _safe_json(value, fallback=None):
    if fallback is None:
        fallback = {}
    if not value:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return fallback


def _redact(value):
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if str(key).lower() in SENSITIVE_KEYS:
                cleaned[key] = "***"
            else:
                cleaned[key] = _redact(item)
        return cleaned
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        if "auth=" in value:
            return value.split("auth=", 1)[0] + "auth=***"
        if len(value) > 220:
            return value[:220] + "..."
    return value


def _file_summary(raw_payload) -> list[dict]:
    raw = _safe_json(raw_payload)
    files = []
    if isinstance(raw, dict):
        if isinstance(raw.get("files"), list):
            files.extend(raw["files"])
        message = raw.get("message")
        if isinstance(message, dict) and isinstance(message.get("files"), list):
            files.extend(message["files"])
        if isinstance(raw.get("attachments"), list):
            files.extend(raw["attachments"])
    result = []
    for item in files[:8]:
        if not isinstance(item, dict):
            continue
        result.append({
            "name": item.get("name") or item.get("NAME") or item.get("filename") or "file",
            "size": item.get("size") or item.get("SIZE"),
            "type": item.get("type") or item.get("content_type") or item.get("CONTENT_TYPE"),
        })
    return result


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic"}
VOICE_EXTENSIONS = {".ogg", ".oga", ".opus", ".mp3", ".wav", ".m4a", ".aac"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv"}


def _filename_ext(name: str) -> str:
    base = str(name or "").lower().split("?", 1)[0]
    if "." not in base:
        return ""
    return "." + base.rsplit(".", 1)[-1]


def _message_media_kind(message: Message | None) -> str:
    if not message:
        return "text"
    text = (message.text or "").strip().lower()
    files = _file_summary(message.raw_payload_json)
    names = [str(item.get("name") or "").lower() for item in files]
    types = [str(item.get("type") or "").lower() for item in files]
    joined = " ".join(names + types)
    if "[стикер" in text or "sticker" in joined:
        return "sticker"
    if "[голосовое" in text or any(_filename_ext(name) in VOICE_EXTENSIONS for name in names) or any("audio/" in kind for kind in types):
        return "voice"
    if "[видео-сообщение" in text or "video_note" in joined or "round_message" in joined:
        return "video_note"
    if files:
        if any(_filename_ext(name) in IMAGE_EXTENSIONS for name in names) or any("image/" in kind for kind in types):
            return "photo"
        if any(_filename_ext(name) in VIDEO_EXTENSIONS for name in names) or any("video/" in kind for kind in types):
            return "video"
        return "file"
    return "text"


def _minutes_delta(later, earlier) -> float | None:
    later = _naive_utc(later)
    earlier = _naive_utc(earlier)
    if not later or not earlier or later <= earlier:
        return None
    return round((later - earlier).total_seconds() / 60, 1)


def _average(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[mid], 1)
    return round((ordered[mid - 1] + ordered[mid]) / 2, 1)


def _raw_payload_dict(message: Message) -> dict:
    raw = _safe_json(message.raw_payload_json)
    return raw if isinstance(raw, dict) else {}


def _payload_source(message: Message) -> str:
    raw = _raw_payload_dict(message)
    return str(raw.get("source") or raw.get("event_source") or "").strip().lower()


def _message_is_widget(message: Message) -> bool:
    return _payload_source(message) == "widget" or (message.sender_name or "").strip() == "Bitrix widget"


def _widget_operator_name(message: Message) -> str | None:
    if _payload_source(message) != "widget":
        return None
    raw = _raw_payload_dict(message)
    operator_name = str(raw.get("operator_name") or "").strip()
    operator_id = str(raw.get("operator_id") or "").strip()
    if operator_name.startswith("Bitrix user #"):
        return UNKNOWN_BITRIX_OPERATOR_NAME
    if operator_name and operator_name != "Bitrix widget":
        return operator_name
    if operator_id:
        return UNKNOWN_BITRIX_OPERATOR_NAME
    return None


def _message_is_test(message: Message) -> bool:
    bitrix_id = str(message.bitrix_message_id or "").lower()
    text = (message.text or "").strip().lower()
    if "synth_" in bitrix_id:
        return True
    return any(text.startswith(prefix) for prefix in TEST_TEXT_PREFIXES)


def _looks_like_bitrix_reply(message: Message) -> bool:
    raw = _raw_payload_dict(message)
    return bool(raw.get("im") or raw.get("message") or message.bitrix_message_id)


def _operator_name_for_stats(message: Message, link: ChatLink | None = None) -> str | None:
    if message.direction != "outbound":
        return None
    if _message_is_test(message):
        return None

    sender = (message.sender_name or "").strip()
    if _message_is_widget(message):
        if sender.startswith("Bitrix user #"):
            sender = UNKNOWN_BITRIX_OPERATOR_NAME
        return _widget_operator_name(message) or (sender if sender and sender not in SERVICE_OPERATOR_NAMES else None)
    if sender in SERVICE_OPERATOR_NAMES:
        return None
    if sender == REAL_OPERATOR_NAME:
        return REAL_OPERATOR_NAME
    if sender in FALLBACK_OPERATOR_NAMES or sender.startswith("Bitrix user #"):
        return UNKNOWN_BITRIX_OPERATOR_NAME
    if not sender and _looks_like_bitrix_reply(message):
        return UNKNOWN_BITRIX_OPERATOR_NAME
    return sender or None


def _message_author_kind(message: Message, link: ChatLink | None = None) -> tuple[str | None, str]:
    operator_name = _operator_name_for_stats(message, link)
    if operator_name:
        return operator_name, "operator"
    if _message_is_widget(message):
        return "Виджет Bitrix", "widget"
    if _message_is_test(message) or (message.sender_name or "").strip() in SERVICE_OPERATOR_NAMES:
        return "Сервис / тест", "service"
    return None, ""


def _message_author(message: Message, link: ChatLink | None) -> str:
    display_name, _kind = _message_author_kind(message, link)
    if display_name:
        return display_name
    if message.sender_name:
        return message.sender_name
    if message.direction == "inbound":
        return (link.external_user_name if link else None) or message.recipient or message.chat_id_tg or "Telegram user"
    return "Bitrix24 / Open Line"


def _message_target(message: Message, link: ChatLink | None) -> str:
    if message.direction == "inbound":
        return f"Open Line {link.bitrix_line_id}" if link and link.bitrix_line_id else "Bitrix24"
    return (link.external_user_name if link else None) or message.recipient or message.chat_id_tg or "Telegram"


def _serialize_message(message: Message, link: ChatLink | None = None, channel: Channel | None = None) -> dict:
    files = _file_summary(message.raw_payload_json)
    author_name, author_kind = _message_author_kind(message, link)
    operator_name = _operator_name_for_stats(message, link)
    return {
        "id": message.id,
        "direction": message.direction,
        "direction_label": "Telegram -> Bitrix24" if message.direction == "inbound" else "Bitrix24 -> Telegram",
        "status": message.status,
        "author": _message_author(message, link),
        "author_kind": author_kind or ("client" if message.direction == "inbound" else "system"),
        "operator": operator_name,
        "is_operator_reply": bool(operator_name),
        "is_service": author_kind in {"widget", "service"},
        "target": _message_target(message, link),
        "text": message.text or "",
        "error": message.error,
        "provider_message_id": message.provider_message_id,
        "bitrix_message_id": message.bitrix_message_id,
        "sender_name": message.sender_name,
        "recipient": message.recipient,
        "account_id": message.account_id,
        "telegram_chat_id": message.chat_id_tg or (link.external_chat_id if link else None),
        "chat_link_id": message.chat_link_id,
        "external_chat_id": link.external_chat_id if link else None,
        "external_user_name": link.external_user_name if link else None,
        "bitrix_line_id": link.bitrix_line_id if link else None,
        "bitrix_chat_id": link.bitrix_chat_id if link else None,
        "crm_entity_type": link.crm_entity_type if link else None,
        "crm_entity_id": link.crm_entity_id if link else None,
        "channel_id": message.channel_id,
        "channel_name": channel.name if channel else None,
        "provider": channel.provider if channel else (link.provider if link else None),
        "source": _payload_source(message),
        "media_type": _message_media_kind(message),
        "files": files,
        "file_count": len(files),
        "created_at": _dt(message.created_at),
        "updated_at": _dt(message.updated_at),
    }


def _message_search_filter(q: str):
    like = f"%{q}%"
    conditions = [
        Message.error.ilike(like),
        Message.sender_name.ilike(like),
        Message.recipient.ilike(like),
        ChatLink.external_user_name.ilike(like),
        ChatLink.external_chat_id.ilike(like),
    ]
    if DATABASE_URL.startswith("postgresql"):
        conditions.append(
            func.to_tsvector(literal_column("'simple'"), func.coalesce(Message.text, "")).op("@@")(
                func.plainto_tsquery(literal_column("'simple'"), q)
            )
        )
    else:
        conditions.append(Message.text.ilike(like))
    return or_(*conditions)


async def _filtered_admin_messages(
    domain: str,
    limit: int = 150,
    status: str = "",
    direction: str = "",
    chat_link_id: int = 0,
    date_from: str = "",
    date_to: str = "",
    operator: str = "",
    client: str = "",
    q: str = "",
    media_type: str = "",
) -> tuple[Tenant | None, list[dict]]:
    tenant = await _tenant_by_domain(domain)
    if not tenant:
        return None, []
    limit = min(max(limit, 1), 5000)
    async with async_session() as session:
        query = (
            select(Message, ChatLink, Channel)
            .outerjoin(ChatLink, Message.chat_link_id == ChatLink.id)
            .outerjoin(Channel, Message.channel_id == Channel.id)
            .where(Message.tenant_id == tenant.id)
        )
        if status:
            query = query.where(Message.status == status)
        if direction:
            query = query.where(Message.direction == direction)
        if chat_link_id:
            link_for_scope = (await session.execute(
                select(ChatLink).where(ChatLink.id == chat_link_id, ChatLink.tenant_id == tenant.id)
            )).scalar_one_or_none()
            if link_for_scope and link_for_scope.external_chat_id:
                same_link_ids = select(ChatLink.id).where(
                    ChatLink.tenant_id == tenant.id,
                    ChatLink.provider == link_for_scope.provider,
                    ChatLink.external_chat_id == link_for_scope.external_chat_id,
                )
                if link_for_scope.account_id is None:
                    same_link_ids = same_link_ids.where(ChatLink.account_id.is_(None))
                    chat_id_scope = and_(
                        Message.chat_id_tg == link_for_scope.external_chat_id,
                        Message.account_id.is_(None),
                    )
                else:
                    same_link_ids = same_link_ids.where(ChatLink.account_id == link_for_scope.account_id)
                    chat_id_scope = and_(
                        Message.chat_id_tg == link_for_scope.external_chat_id,
                        Message.account_id == link_for_scope.account_id,
                    )
                query = query.where(or_(
                    Message.chat_link_id.in_(same_link_ids),
                    chat_id_scope,
                ))
            else:
                query = query.where(Message.chat_link_id == chat_link_id)
        start = _parse_date_bound(date_from)
        end = _parse_date_bound(date_to, end=True)
        if start:
            query = query.where(Message.created_at >= start)
        if end:
            query = query.where(Message.created_at < end)
        if operator:
            query = query.where(Message.direction == "outbound")
        if client:
            like_client = f"%{client}%"
            query = query.where(or_(
                ChatLink.external_user_name.ilike(like_client),
                ChatLink.external_chat_id.ilike(like_client),
                Message.recipient.ilike(like_client),
                Message.chat_id_tg.ilike(like_client),
            ))
        if q:
            query = query.where(_message_search_filter(q))
        fetch_limit = min(5000, max(limit, 500)) if (operator or media_type) else limit
        result = await session.execute(query.order_by(Message.created_at.desc()).limit(fetch_limit))
        rows = result.all()
    messages = [_serialize_message(m, l, c) for m, l, c in rows]
    if operator:
        needle = operator.strip().lower()
        messages = [
            item for item in messages
            if needle in str(item.get("operator") or item.get("author") or item.get("sender_name") or "").lower()
        ]
    if media_type:
        media_needle = media_type.strip().lower()
        messages = [item for item in messages if str(item.get("media_type") or "").lower() == media_needle]
    messages = messages[:limit]
    return tenant, messages


def _serialize_outbox(entry: OutboxEntry) -> dict:
    payload = _redact(_safe_json(entry.payload_json))
    return {
        "id": entry.id,
        "direction": entry.direction,
        "status": entry.status,
        "attempts": entry.attempts,
        "max_retries": entry.max_retries,
        "last_error": entry.last_error,
        "payload": payload,
        "created_at": _dt(entry.created_at),
        "updated_at": _dt(entry.updated_at),
        "next_retry_at": _dt(entry.next_retry_at),
    }


def _serialize_quick_reply(item: QuickReplyTemplate) -> dict:
    return {
        "id": item.id,
        "title": item.title,
        "text": item.text,
        "sort_order": item.sort_order,
        "active": bool(item.active),
        "created_at": _dt(item.created_at),
        "updated_at": _dt(item.updated_at),
    }


async def _lead_audit_row(session, tenant: Tenant, link: ChatLink, channel: Channel | None = None) -> dict:
    message_count = (await session.execute(
        select(func.count(Message.id)).where(
            Message.tenant_id == tenant.id,
            Message.chat_link_id == link.id,
        )
    )).scalar_one()
    latest = (await session.execute(
        select(Message).where(
            Message.tenant_id == tenant.id,
            Message.chat_link_id == link.id,
        ).order_by(Message.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    first = (await session.execute(
        select(Message).where(
            Message.tenant_id == tenant.id,
            Message.chat_link_id == link.id,
        ).order_by(Message.created_at.asc()).limit(1)
    )).scalar_one_or_none()
    crm_id = link.crm_entity_id or ""
    bitrix_chat_id = link.bitrix_chat_id or ""
    return {
        "id": link.id,
        "client": link.external_user_name or link.external_chat_id,
        "external_chat_id": link.external_chat_id,
        "external_user_id": link.external_user_id,
        "provider": link.provider,
        "bitrix_line_id": link.bitrix_line_id,
        "bitrix_chat_id": bitrix_chat_id,
        "bitrix_dialog_id": link.bitrix_dialog_id,
        "crm_entity_type": link.crm_entity_type,
        "crm_entity_id": crm_id,
        "has_lead": bool(crm_id),
        "can_create_lead": bool(bitrix_chat_id and not crm_id),
        "lead_url": lead_url(tenant.bitrix_domain, crm_id) if crm_id else "",
        "chat_url": openline_chat_url(tenant.bitrix_domain, bitrix_chat_id) if bitrix_chat_id else "",
        "channel_id": link.channel_id,
        "channel_name": channel.name if channel else None,
        "message_count": message_count,
        "latest_text": latest.text if latest else "",
        "latest_status": latest.status if latest else "",
        "latest_at": _dt(latest.created_at if latest else None),
        "first_text": first.text if first else "",
        "created_at": _dt(link.created_at),
        "updated_at": _dt(link.updated_at),
    }


async def _tenant_by_domain(domain: str):
    async with async_session() as session:
        result = await session.execute(select(Tenant).where(Tenant.bitrix_domain == domain))
        return result.scalar_one_or_none()


async def _connector_status(tenant: Tenant) -> dict:
    try:
        from app.crypto import decrypt_token
        import httpx
        token = decrypt_token(tenant.access_token_encrypted)
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"https://{tenant.bitrix_domain}/rest/imconnector.status",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "CONNECTOR": settings.primary_connector_id or settings.connector_id,
                    "LINE": settings.primary_bitrix_line_id,
                },
            )
        data = response.json()
        return data.get("result") or data
    except Exception as exc:
        return {"ERROR": True, "message": str(exc)}


def _health_check(code: str, title: str, status: str, summary: str, details=None, action: str = "") -> dict:
    return {
        "code": code,
        "title": title,
        "status": status,
        "summary": summary,
        "details": details or {},
        "action": action,
    }


def _health_overall(checks: list[dict]) -> str:
    statuses = {item.get("status") for item in checks}
    if "error" in statuses:
        return "error"
    if "warn" in statuses:
        return "warn"
    return "ok"


def _status_for_bool(value) -> str:
    return "ok" if value else "warn"


def _binding_event_name(binding: dict) -> str:
    for key in ("event", "EVENT", "event_name", "EVENT_NAME"):
        if binding.get(key):
            return str(binding.get(key)).upper()
    return ""


def _binding_handler(binding: dict) -> str:
    for key in ("handler", "HANDLER", "url", "URL", "handler_url", "HANDLER_URL"):
        if binding.get(key):
            return str(binding.get(key))
    return ""


def _extract_event_bindings(result) -> list[dict]:
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if not isinstance(result, dict):
        return []

    for key in ("events", "EVENTS", "bindings", "BINDINGS", "result", "RESULT"):
        nested = result.get(key)
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
        if isinstance(nested, dict):
            return _extract_event_bindings(nested)

    bindings = []
    for key, value in result.items():
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    item = dict(item)
                    item.setdefault("event", key)
                    bindings.append(item)
        elif isinstance(value, dict):
            item = dict(value)
            item.setdefault("event", key)
            bindings.append(item)
    return bindings


def _line_id(line: dict):
    for key in ("ID", "id", "LINE_ID", "line_id"):
        if line.get(key) is not None:
            return str(line.get(key))
    return ""


def _line_name(line: dict) -> str:
    for key in ("LINE_NAME", "NAME", "name", "TITLE", "title"):
        if line.get(key):
            return str(line.get(key))
    return ""


def _line_summary(line: dict) -> dict:
    keys = (
        "ID",
        "ACTIVE",
        "LINE_NAME",
        "CRM",
        "CRM_CREATE",
        "CRM_CHAT_TRACKER",
        "QUEUE_TYPE",
        "MODIFY_USER_ID",
    )
    return {key: line.get(key) for key in keys if line.get(key) is not None}


def _extract_open_lines(result) -> list[dict]:
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if not isinstance(result, dict):
        return []

    for key in ("LINES", "lines", "result", "RESULT"):
        nested = result.get(key)
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
        if isinstance(nested, dict):
            return _extract_open_lines(nested)

    lines = []
    for key, value in result.items():
        if isinstance(value, dict):
            item = dict(value)
            item.setdefault("ID", key)
            lines.append(item)
    return lines


async def _bitrix_diagnostic_call(domain: str, token: str, method: str, params: dict | None = None) -> dict:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            payload = dict(params or {})
            response = await client.post(
                f"https://{domain}/rest/{method}",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
        data = response.json()
    except Exception as exc:
        return {"ok": False, "method": method, "error": str(exc), "result": None}

    if data.get("error"):
        return {
            "ok": False,
            "method": method,
            "error": data.get("error_description") or data.get("error"),
            "result": None,
            "raw": _redact(data),
        }
    return {"ok": True, "method": method, "result": data.get("result"), "raw": _redact(data)}


async def _collect_bitrix_health(tenant: Tenant) -> dict:
    from app.crypto import decrypt_token

    try:
        token = decrypt_token(tenant.access_token_encrypted)
    except Exception as exc:
        return {
            "api_ok": False,
            "errors": [f"Token decrypt failed: {exc}"],
            "connector_status": {},
            "events": {},
            "open_line": {},
        }

    connector_id = settings.primary_connector_id or settings.connector_id
    line_id = settings.primary_bitrix_line_id
    connector_call = await _bitrix_diagnostic_call(
        tenant.bitrix_domain,
        token,
        "imconnector.status",
        {"CONNECTOR": connector_id, "LINE": line_id},
    )
    events_call = await _bitrix_diagnostic_call(tenant.bitrix_domain, token, "event.get", {})
    lines_call = await _bitrix_diagnostic_call(tenant.bitrix_domain, token, "imopenlines.config.list.get", {})

    bindings = _extract_event_bindings(events_call.get("result"))
    expected_handler = f"{settings.app_url}/bitrix/events"
    event_map = {}
    for event in EXPECTED_BITRIX_EVENTS + OPTIONAL_BITRIX_EVENTS:
        found = []
        for binding in bindings:
            if _binding_event_name(binding) != event:
                continue
            handler = _binding_handler(binding)
            found.append({"handler": handler, "matches_app": expected_handler in handler})
        event_map[event] = {
            "bound": bool(found),
            "handler_ok": any(item["matches_app"] for item in found),
            "handlers": found[:5],
        }

    lines = _extract_open_lines(lines_call.get("result"))
    target_line = None
    for line in lines:
        if _line_id(line) == str(line_id):
            target_line = line
            break

    errors = [
        item.get("error") for item in (connector_call, events_call, lines_call)
        if not item.get("ok")
    ]
    connector_status = connector_call.get("result") if connector_call.get("ok") else {}
    return {
        "api_ok": not errors,
        "errors": [err for err in errors if err],
        "connector_status": connector_status if isinstance(connector_status, dict) else {"raw": connector_status},
        "events": {
            "expected_handler": expected_handler,
            "items": event_map,
            "raw_count": len(bindings),
            "call_ok": events_call.get("ok"),
        },
        "open_line": {
            "id": line_id,
            "found": bool(target_line),
            "name": _line_name(target_line or {}),
            "active": (target_line or {}).get("ACTIVE") or (target_line or {}).get("active"),
            "raw": _redact(_line_summary(target_line or {})),
            "call_ok": lines_call.get("ok"),
        },
    }


@router.get("/admin")
async def admin_page():
    html = _build_admin_html(settings.connector_name, settings.connector_id)
    return HTMLResponse(content=html, headers={
        "X-Frame-Options": "ALLOWALL",
        "Content-Security-Policy": "frame-ancestors *",
    })


@router.get("/api/admin/tenants")
async def admin_tenants():
    async with async_session() as session:
        result = await session.execute(select(Tenant).order_by(Tenant.created_at.desc()))
        tenants = result.scalars().all()
    return {
        "success": True,
        "tenants": [
            {
                "id": t.id,
                "domain": t.bitrix_domain,
                "status": t.status,
                "member_id": t.member_id,
                "install_user_id": t.install_user_id,
                "created_at": _dt(t.created_at),
                "updated_at": _dt(t.updated_at),
            }
            for t in tenants
        ],
    }


@router.get("/api/admin/overview")
async def admin_overview(domain: str = ""):
    tenant = await _tenant_by_domain(domain) if domain else None
    if not tenant:
        return {"success": False, "message": "Tenant not found"}

    async with async_session() as session:
        msg_counts = dict((await session.execute(
            select(Message.status, func.count(Message.id))
            .where(Message.tenant_id == tenant.id)
            .group_by(Message.status)
        )).all())
        direction_counts = dict((await session.execute(
            select(Message.direction, func.count(Message.id))
            .where(Message.tenant_id == tenant.id)
            .group_by(Message.direction)
        )).all())
        today = utcnow() - timedelta(hours=24)
        recent_count = (await session.execute(
            select(func.count(Message.id)).where(Message.tenant_id == tenant.id, Message.created_at >= today)
        )).scalar_one()
        channel_count = (await session.execute(
            select(func.count(Channel.id)).where(Channel.tenant_id == tenant.id)
        )).scalar_one()
        dialog_count = (await session.execute(
            select(func.count(ChatLink.id)).where(ChatLink.tenant_id == tenant.id)
        )).scalar_one()
        account_count = (await session.execute(
            select(func.count(TelegramAccount.id)).where(TelegramAccount.tenant_id == tenant.id)
        )).scalar_one()
        active_account_count = (await session.execute(
            select(func.count(TelegramAccount.id)).where(
                TelegramAccount.tenant_id == tenant.id,
                TelegramAccount.state.in_(["active", "authorized"]),
            )
        )).scalar_one()
        outbox_counts = dict((await session.execute(
            select(OutboxEntry.status, func.count(OutboxEntry.id)).group_by(OutboxEntry.status)
        )).all())
        latest_result = await session.execute(
            select(Message, ChatLink, Channel)
            .outerjoin(ChatLink, Message.chat_link_id == ChatLink.id)
            .outerjoin(Channel, Message.channel_id == Channel.id)
            .where(Message.tenant_id == tenant.id)
            .order_by(Message.created_at.desc())
            .limit(8)
        )
        latest = [_serialize_message(m, l, c) for m, l, c in latest_result.all()]
        trend_start = utcnow() - timedelta(days=7)
        trend_rows = (await session.execute(
            select(Message.created_at, Message.direction, Message.status)
            .where(Message.tenant_id == tenant.id, Message.created_at >= trend_start)
        )).all()
        trend_messages = (await session.execute(
            select(Message)
            .where(Message.tenant_id == tenant.id, Message.created_at >= trend_start)
            .order_by(Message.created_at.desc())
            .limit(5000)
        )).scalars().all()
        dialog_trend_rows = (await session.execute(
            select(ChatLink.created_at)
            .where(ChatLink.tenant_id == tenant.id, ChatLink.created_at >= trend_start)
        )).all()
        latest_message_rows = (await session.execute(
            select(Message)
            .where(Message.tenant_id == tenant.id, Message.chat_link_id.is_not(None))
            .order_by(Message.created_at.desc())
            .limit(10000)
        )).scalars().all()
        trend_days = [
            (datetime.now(MSK).date() - timedelta(days=offset)).isoformat()
            for offset in range(6, -1, -1)
        ]
        trends = {
            "messages": {day: 0 for day in trend_days},
            "inbound": {day: 0 for day in trend_days},
            "outbound": {day: 0 for day in trend_days},
            "errors": {day: 0 for day in trend_days},
            "dialogs": {day: 0 for day in trend_days},
        }
        for created_at, direction, status in trend_rows:
            day = _local_day(created_at)
            if day not in trends["messages"]:
                continue
            trends["messages"][day] += 1
            if direction in ("inbound", "outbound"):
                trends[direction][day] += 1
            if status in ("failed", "no_link", "edit_failed"):
                trends["errors"][day] += 1
        for (created_at,) in dialog_trend_rows:
            day = _local_day(created_at)
            if day in trends["dialogs"]:
                trends["dialogs"][day] += 1

        media_distribution = {key: 0 for key in ("text", "photo", "voice", "sticker", "file", "video", "video_note")}
        heatmap = [[0 for _ in range(24)] for _ in range(7)]
        client_inbound_counts: dict[str, int] = {}
        for message in trend_messages:
            kind = _message_media_kind(message)
            media_distribution[kind if kind in media_distribution else "file"] += 1
            if message.direction == "inbound" and message.created_at:
                created_at = message.created_at
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                local_dt = created_at.astimezone(MSK)
                heatmap[local_dt.weekday()][local_dt.hour] += 1
                client_key = message.chat_id_tg or message.recipient or ""
                if client_key:
                    client_inbound_counts[client_key] = client_inbound_counts.get(client_key, 0) + 1
        repeat_clients = sum(1 for count in client_inbound_counts.values() if count > 1)

        latest_by_link = {}
        for message in latest_message_rows:
            if message.chat_link_id and message.chat_link_id not in latest_by_link:
                latest_by_link[message.chat_link_id] = message
        waiting_threshold = 30
        now = _naive_utc(utcnow())
        waiting_active_window_days = 7
        waiting_active_since = now - timedelta(days=waiting_active_window_days)
        waiting_dialogs = 0
        waiting_over_threshold = 0
        waiting_archived_over_threshold = 0
        waiting_max_minutes = 0
        for message in latest_by_link.values():
            if message.direction != "inbound" or not message.created_at:
                continue
            created_at = _naive_utc(message.created_at)
            if not created_at:
                continue
            minutes = max(0, int((now - created_at).total_seconds() // 60))
            waiting_max_minutes = max(waiting_max_minutes, minutes)
            if created_at >= waiting_active_since:
                waiting_dialogs += 1
                if minutes >= waiting_threshold:
                    waiting_over_threshold += 1
            elif minutes >= waiting_threshold:
                waiting_archived_over_threshold += 1

    return {
        "success": True,
        "tenant": {
            "domain": tenant.bitrix_domain,
            "status": tenant.status,
            "member_id": tenant.member_id,
            "install_user_id": tenant.install_user_id,
        },
        "connector": await _connector_status(tenant),
        "counts": {
            "messages": sum(msg_counts.values()),
            "messages_24h": recent_count,
            "inbound": direction_counts.get("inbound", 0),
            "outbound": direction_counts.get("outbound", 0),
            "sent": msg_counts.get("sent", 0),
            "queued": msg_counts.get("queued", 0),
            "failed": msg_counts.get("failed", 0),
            "no_link": msg_counts.get("no_link", 0),
            "channels": channel_count,
            "dialogs": dialog_count,
            "accounts": account_count,
            "active_accounts": active_account_count,
            "outbox_pending": outbox_counts.get("pending", 0) + outbox_counts.get("processing", 0),
            "outbox_failed": outbox_counts.get("failed", 0),
            "outbox_sent": outbox_counts.get("sent", 0),
            "waiting_dialogs": waiting_dialogs,
            "waiting_over_30": waiting_over_threshold,
            "waiting_archived_over_30": waiting_archived_over_threshold,
            "waiting_active_window_days": waiting_active_window_days,
            "waiting_max_minutes": waiting_max_minutes,
            "waiting_threshold_minutes": waiting_threshold,
            "repeat_clients_7d": repeat_clients,
        },
        "latest_messages": latest,
        "trends": {key: list(values.values()) for key, values in trends.items()},
        "trend_labels": trend_days,
        "heatmap": {
            "days": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
            "hours": list(range(24)),
            "values": heatmap,
        },
        "media_distribution": media_distribution,
    }


@router.get("/api/admin/health")
async def admin_health(domain: str = ""):
    tenant = await _tenant_by_domain(domain) if domain else None
    if not tenant:
        return {"success": False, "message": "Tenant not found"}

    try:
        from app.telegram.service import _active_clients
        active_client_ids = set(_active_clients.keys())
    except Exception:
        active_client_ids = set()

    now = utcnow()
    since_24h = now - timedelta(hours=24)
    primary_line_id = settings.primary_bitrix_line_id
    primary_connector_id = settings.primary_connector_id or settings.connector_id

    async with async_session() as session:
        accounts = (await session.execute(
            select(TelegramAccount)
            .where(TelegramAccount.tenant_id == tenant.id)
            .order_by(TelegramAccount.id.asc())
        )).scalars().all()
        msg_counts = dict((await session.execute(
            select(Message.status, func.count(Message.id))
            .where(Message.tenant_id == tenant.id)
            .group_by(Message.status)
        )).all())
        recent_problem_count = (await session.execute(
            select(func.count(Message.id)).where(
                Message.tenant_id == tenant.id,
                Message.status.in_(["failed", "no_link", "edit_failed"]),
                Message.created_at >= since_24h,
            )
        )).scalar_one()
        recent_message_count = (await session.execute(
            select(func.count(Message.id)).where(
                Message.tenant_id == tenant.id,
                Message.created_at >= since_24h,
            )
        )).scalar_one()
        link_total = (await session.execute(
            select(func.count(ChatLink.id)).where(ChatLink.tenant_id == tenant.id)
        )).scalar_one()
        links_on_primary = (await session.execute(
            select(func.count(ChatLink.id)).where(
                ChatLink.tenant_id == tenant.id,
                ChatLink.bitrix_line_id == primary_line_id,
            )
        )).scalar_one()
        links_wrong_line = (await session.execute(
            select(func.count(ChatLink.id)).where(
                ChatLink.tenant_id == tenant.id,
                ChatLink.bitrix_line_id.is_not(None),
                ChatLink.bitrix_line_id != primary_line_id,
            )
        )).scalar_one()
        links_without_chat = (await session.execute(
            select(func.count(ChatLink.id)).where(
                ChatLink.tenant_id == tenant.id,
                ChatLink.bitrix_line_id == primary_line_id,
                or_(ChatLink.bitrix_chat_id.is_(None), ChatLink.bitrix_chat_id == ""),
            )
        )).scalar_one()
        links_without_chat_rows = (await session.execute(
            select(ChatLink).where(
                ChatLink.tenant_id == tenant.id,
                ChatLink.bitrix_line_id == primary_line_id,
                or_(ChatLink.bitrix_chat_id.is_(None), ChatLink.bitrix_chat_id == ""),
            )
        )).scalars().all()
        links_without_chat_with_messages = 0
        empty_orphan_links = 0
        for link in links_without_chat_rows:
            link_messages = (await session.execute(
                select(func.count(Message.id)).where(
                    Message.tenant_id == tenant.id,
                    Message.chat_link_id == link.id,
                )
            )).scalar_one()
            if link_messages:
                links_without_chat_with_messages += 1
            elif not link.crm_entity_id:
                empty_orphan_links += 1
        links_without_lead = (await session.execute(
            select(func.count(ChatLink.id)).where(
                ChatLink.tenant_id == tenant.id,
                ChatLink.bitrix_line_id == primary_line_id,
                or_(ChatLink.crm_entity_id.is_(None), ChatLink.crm_entity_id == ""),
            )
        )).scalar_one()
        outbox_counts = dict((await session.execute(
            select(OutboxEntry.status, func.count(OutboxEntry.id)).group_by(OutboxEntry.status)
        )).all())
        stuck_processing = (await session.execute(
            select(func.count(OutboxEntry.id)).where(
                OutboxEntry.status == "processing",
                OutboxEntry.updated_at < now - timedelta(minutes=10),
            )
        )).scalar_one()
        message_errors = (await session.execute(
            select(Message, ChatLink, Channel)
            .outerjoin(ChatLink, Message.chat_link_id == ChatLink.id)
            .outerjoin(Channel, Message.channel_id == Channel.id)
            .where(
                Message.tenant_id == tenant.id,
                Message.status.in_(["failed", "no_link", "edit_failed"]),
            )
            .order_by(Message.updated_at.desc())
            .limit(8)
        )).all()
        outbox_errors = (await session.execute(
            select(OutboxEntry)
            .where(OutboxEntry.status == "failed")
            .order_by(OutboxEntry.updated_at.desc())
            .limit(8)
        )).scalars().all()

    bitrix = await _collect_bitrix_health(tenant)

    active_accounts = [a for a in accounts if a.state in {"active", "authorized"}]
    listening_accounts = [a for a in accounts if a.id in active_client_ids]
    accounts_wrong_line = [
        a.id for a in active_accounts
        if a.bitrix_line_id and a.bitrix_line_id != primary_line_id
    ]
    accounts_wrong_connector = [
        a.id for a in active_accounts
        if a.bitrix_connector_id and a.bitrix_connector_id != primary_connector_id
    ]

    checks = []
    token_expires = tenant.expires_at
    token_status = "ok"
    token_summary = "OAuth token saved"
    if token_expires:
        token_expires_cmp = _naive_utc(token_expires)
        if token_expires_cmp <= now:
            token_status = "warn"
            token_summary = "OAuth token may be expired"
    checks.append(_health_check(
        "tenant_token",
        "Bitrix OAuth",
        token_status,
        token_summary,
        {"expires_at": _dt(token_expires), "tenant_status": tenant.status},
    ))

    checks.append(_health_check(
        "bitrix_api",
        "Bitrix API",
        "ok" if bitrix.get("api_ok") else "error",
        "Bitrix methods respond" if bitrix.get("api_ok") else "Bitrix diagnostic calls failed",
        {"errors": bitrix.get("errors", [])},
    ))

    connector_status = bitrix.get("connector_status") or {}
    connector_known = bool(connector_status) and not connector_status.get("ERROR")
    checks.append(_health_check(
        "connector",
        "Connector",
        "ok" if connector_known else "warn",
        f"{primary_connector_id} / line {primary_line_id}",
        connector_status,
    ))

    event_items = (bitrix.get("events") or {}).get("items") or {}
    missing_events = [
        name for name in EXPECTED_BITRIX_EVENTS
        if not (event_items.get(name) or {}).get("handler_ok")
    ]
    event_call_ok = (bitrix.get("events") or {}).get("call_ok")
    events_status = "ok" if event_call_ok and not missing_events else ("error" if not event_call_ok else "warn")
    checks.append(_health_check(
        "events",
        "Bitrix events",
        events_status,
        "All required event handlers are bound" if not missing_events else f"Missing handlers: {', '.join(missing_events)}",
        {"missing": missing_events, "expected": EXPECTED_BITRIX_EVENTS},
        "Переустановить приложение, если обработчики отсутствуют" if events_status != "ok" else "",
    ))

    line = bitrix.get("open_line") or {}
    line_active = line.get("active")
    line_ok = line.get("found") and (line_active in (None, True, "Y", "1", 1) or str(line_active).lower() == "true")
    checks.append(_health_check(
        "open_line",
        "Open Line 30",
        "ok" if line_ok else ("error" if not line.get("found") else "warn"),
        line.get("name") or f"Line {primary_line_id}",
        line,
    ))

    if not active_accounts:
        account_status = "error"
        account_summary = "No active Telegram accounts"
    elif not listening_accounts:
        account_status = "error"
        account_summary = "Telegram accounts are authorized but not listening"
    elif accounts_wrong_line or accounts_wrong_connector:
        account_status = "warn"
        account_summary = "Some accounts are not attached to the primary line/connector"
    else:
        account_status = "ok"
        account_summary = f"{len(listening_accounts)} account(s) listening"
    checks.append(_health_check(
        "telegram_accounts",
        "Telegram accounts",
        account_status,
        account_summary,
        {
            "active": len(active_accounts),
            "listening": len(listening_accounts),
            "wrong_line": accounts_wrong_line,
            "wrong_connector": accounts_wrong_connector,
        },
        "Запустить все аккаунты" if account_status != "ok" else "",
    ))

    checks.append(_health_check(
        "chat_links",
        "Dialog links",
        "warn" if links_wrong_line or links_without_chat_with_messages else "ok",
        f"{links_on_primary}/{link_total} dialogs on line {primary_line_id}",
        {
            "total": link_total,
            "on_primary_line": links_on_primary,
            "wrong_line": links_wrong_line,
            "without_bitrix_chat_id": links_without_chat,
            "without_bitrix_chat_with_messages": links_without_chat_with_messages,
            "empty_orphan_links": empty_orphan_links,
            "without_lead": links_without_lead,
        },
        "Очистить пустые связки" if empty_orphan_links else "",
    ))

    total_problem_messages = sum(msg_counts.get(key, 0) for key in ("failed", "no_link", "edit_failed"))
    checks.append(_health_check(
        "messages",
        "Messages",
        "warn" if recent_problem_count else "ok",
        f"{recent_message_count} messages in 24h, {recent_problem_count} recent problem(s)",
        {"statuses": msg_counts, "problem_total": total_problem_messages},
    ))

    outbox_problem = outbox_counts.get("failed", 0) + stuck_processing
    checks.append(_health_check(
        "outbox",
        "Outbox",
        "warn" if outbox_problem else "ok",
        f"{outbox_counts.get('pending', 0)} pending, {outbox_counts.get('failed', 0)} failed",
        {"statuses": outbox_counts, "stuck_processing": stuck_processing},
    ))

    return {
        "success": True,
        "generated_at": _dt(now),
        "overall": _health_overall(checks),
        "primary": {
            "line_id": primary_line_id,
            "connector_id": primary_connector_id,
        },
        "checks": checks,
        "bitrix": bitrix,
        "telegram_accounts": [
            {
                "id": a.id,
                "username": a.username,
                "phone": a.phone,
                "state": a.state,
                "bitrix_line_id": a.bitrix_line_id,
                "bitrix_connector_id": a.bitrix_connector_id,
                "is_listening": a.id in active_client_ids,
                "updated_at": _dt(a.updated_at),
            }
            for a in accounts
        ],
        "database": {
            "messages_24h": recent_message_count,
            "message_statuses": msg_counts,
            "links_total": link_total,
            "links_on_primary_line": links_on_primary,
            "links_wrong_line": links_wrong_line,
            "links_without_bitrix_chat": links_without_chat,
            "links_without_bitrix_chat_with_messages": links_without_chat_with_messages,
            "empty_orphan_links": empty_orphan_links,
            "links_without_lead": links_without_lead,
            "outbox_statuses": outbox_counts,
        },
        "errors": {
            "messages": [_serialize_message(m, l, c) for m, l, c in message_errors],
            "outbox": [_serialize_outbox(e) for e in outbox_errors],
        },
    }


@router.post("/api/admin/actions/rebind-events")
async def admin_rebind_events(request: Request):
    body = await request.json()
    domain = (body.get("domain") or "").strip()
    force = bool(body.get("force"))
    bind_optional = bool(body.get("bind_optional"))
    tenant = await _tenant_by_domain(domain) if domain else None
    if not tenant:
        return {"success": False, "message": "Tenant not found"}

    health = await _collect_bitrix_health(tenant)
    event_items = (health.get("events") or {}).get("items") or {}
    missing = [
        event for event in EXPECTED_BITRIX_EVENTS
        if force or not (event_items.get(event) or {}).get("handler_ok")
    ]
    if bind_optional:
        missing.extend([
            event for event in OPTIONAL_BITRIX_EVENTS
            if force or not (event_items.get(event) or {}).get("handler_ok")
        ])
    handler = f"{settings.app_url}/bitrix/events"
    results = []
    for event in missing:
        try:
            result = await bx.bind_event(domain, tenant.access_token_encrypted, event, handler)
            results.append({"event": event, "success": result is not None, "result": _redact(result)})
        except Exception as exc:
            results.append({"event": event, "success": False, "error": str(exc)})

    return {
        "success": all(item.get("success") for item in results) if results else True,
        "message": "Events rebound" if results else "All handlers already bound",
        "handler": handler,
        "bound": len([item for item in results if item.get("success")]),
        "missing_before": missing,
        "results": results,
    }


@router.post("/api/admin/actions/retry-failed-outbox")
async def admin_retry_failed_outbox(request: Request):
    body = await request.json()
    limit = min(max(int(body.get("limit") or 50), 1), 200)
    now = utcnow()
    async with async_session() as session:
        entries = (await session.execute(
            select(OutboxEntry.id)
            .where(OutboxEntry.status == "failed")
            .order_by(OutboxEntry.updated_at.desc())
            .limit(limit)
        )).all()
        ids = [entry_id for (entry_id,) in entries]
        if ids:
            await session.execute(
                sa_update(OutboxEntry)
                .where(OutboxEntry.id.in_(ids))
                .values(status="pending", attempts=0, last_error=None, next_retry_at=now)
            )
            await session.commit()
    return {"success": True, "message": f"Queued {len(ids)} failed outbox entries", "ids": ids}


@router.post("/api/admin/actions/skip-outbox/{entry_id}")
async def admin_skip_outbox(entry_id: int):
    async with async_session() as session:
        entry = await session.get(OutboxEntry, entry_id)
        if not entry or entry.status != "failed":
            return {"success": False, "message": "Entry not found or not failed"}
        entry.status = "skipped"
        entry.last_error = f"Skipped by admin. Previous error: {entry.last_error or ''}".strip()
        entry.next_retry_at = None
        await session.commit()
    return {"success": True, "message": f"Outbox #{entry_id} skipped"}


@router.post("/api/admin/actions/cleanup-empty-links")
async def admin_cleanup_empty_links(request: Request):
    body = await request.json()
    domain = (body.get("domain") or "").strip()
    dry_run = bool(body.get("dry_run"))
    tenant = await _tenant_by_domain(domain) if domain else None
    if not tenant:
        return {"success": False, "message": "Tenant not found", "deleted": 0, "candidates": []}

    primary_line_id = settings.primary_bitrix_line_id
    async with async_session() as session:
        links = (await session.execute(
            select(ChatLink).where(
                ChatLink.tenant_id == tenant.id,
                ChatLink.bitrix_line_id == primary_line_id,
                or_(ChatLink.bitrix_chat_id.is_(None), ChatLink.bitrix_chat_id == ""),
                or_(ChatLink.crm_entity_id.is_(None), ChatLink.crm_entity_id == ""),
            )
        )).scalars().all()

        candidates = []
        for link in links:
            message_count = (await session.execute(
                select(func.count(Message.id)).where(
                    Message.tenant_id == tenant.id,
                    Message.chat_link_id == link.id,
                )
            )).scalar_one()
            if message_count:
                continue
            candidates.append({
                "id": link.id,
                "external_chat_id": link.external_chat_id,
                "external_user_name": link.external_user_name,
                "created_at": _dt(link.created_at),
                "updated_at": _dt(link.updated_at),
            })

        ids = [item["id"] for item in candidates]
        if ids and not dry_run:
            await session.execute(sa_delete(ChatLink).where(ChatLink.id.in_(ids)))
            await session.commit()

    return {
        "success": True,
        "message": f"{'Found' if dry_run else 'Deleted'} {len(ids)} empty links",
        "deleted": 0 if dry_run else len(ids),
        "candidates": candidates,
        "dry_run": dry_run,
    }


@router.get("/api/admin/leads-audit")
async def admin_leads_audit(
    domain: str,
    status: str = "",
    q: str = "",
    limit: int = 200,
):
    tenant = await _tenant_by_domain(domain)
    if not tenant:
        return {"success": False, "message": "Tenant not found", "rows": [], "totals": {}}
    limit = min(max(limit, 1), 500)
    async with async_session() as session:
        query = (
            select(ChatLink, Channel)
            .outerjoin(Channel, ChatLink.channel_id == Channel.id)
            .where(ChatLink.tenant_id == tenant.id)
        )
        if status == "without_lead":
            query = query.where(or_(ChatLink.crm_entity_id.is_(None), ChatLink.crm_entity_id == ""))
        elif status == "with_lead":
            query = query.where(ChatLink.crm_entity_id.is_not(None), ChatLink.crm_entity_id != "")
        elif status == "can_create":
            query = query.where(
                ChatLink.bitrix_chat_id.is_not(None),
                ChatLink.bitrix_chat_id != "",
                or_(ChatLink.crm_entity_id.is_(None), ChatLink.crm_entity_id == ""),
            )
        if q:
            like = f"%{q}%"
            query = query.where(or_(
                ChatLink.external_user_name.ilike(like),
                ChatLink.external_chat_id.ilike(like),
                ChatLink.bitrix_chat_id.ilike(like),
                ChatLink.crm_entity_id.ilike(like),
            ))
        rows = (await session.execute(query.order_by(ChatLink.updated_at.desc()).limit(limit))).all()
        serialized = [await _lead_audit_row(session, tenant, link, channel) for link, channel in rows]
        total = (await session.execute(
            select(func.count(ChatLink.id)).where(ChatLink.tenant_id == tenant.id)
        )).scalar_one()
        with_lead = (await session.execute(
            select(func.count(ChatLink.id)).where(
                ChatLink.tenant_id == tenant.id,
                ChatLink.crm_entity_id.is_not(None),
                ChatLink.crm_entity_id != "",
            )
        )).scalar_one()
        can_create = (await session.execute(
            select(func.count(ChatLink.id)).where(
                ChatLink.tenant_id == tenant.id,
                ChatLink.bitrix_chat_id.is_not(None),
                ChatLink.bitrix_chat_id != "",
                or_(ChatLink.crm_entity_id.is_(None), ChatLink.crm_entity_id == ""),
            )
        )).scalar_one()
    return {
        "success": True,
        "rows": serialized,
        "totals": {
            "total": total,
            "with_lead": with_lead,
            "without_lead": total - with_lead,
            "can_create": can_create,
        },
    }


@router.post("/api/admin/actions/create-lead-for-dialog")
async def admin_create_lead_for_dialog(request: Request):
    body = await request.json()
    domain = (body.get("domain") or "").strip()
    chat_link_id = int(body.get("chat_link_id") or 0)
    native = bool(body.get("native"))
    tenant = await _tenant_by_domain(domain) if domain else None
    if not tenant or not chat_link_id:
        return {"success": False, "message": "Missing tenant or chat_link_id"}

    async with async_session() as session:
        link = await session.get(ChatLink, chat_link_id)
        if not link or link.tenant_id != tenant.id:
            return {"success": False, "message": "Dialog not found"}
        first = (await session.execute(
            select(Message).where(Message.chat_link_id == link.id)
            .order_by(Message.created_at.asc()).limit(1)
        )).scalar_one_or_none()
        display_name = link.external_user_name or link.external_chat_id or "Telegram client"
        recipient = link.external_user_name or link.external_chat_id or ""
        first_text = first.text if first else ""
        connector_id = link.provider or settings.primary_connector_id or settings.connector_id
        bitrix_chat_id = link.bitrix_chat_id
        existing_lead_id = link.crm_entity_id

    if existing_lead_id:
        note = await ensure_openline_timeline_note(
            domain, tenant.access_token_encrypted, str(existing_lead_id), bitrix_chat_id,
            display_name=display_name, recipient=recipient, first_text=first_text,
        )
        return {
            "success": True,
            "message": "Lead already linked",
            "id": str(existing_lead_id),
            "created": False,
            "timeline_note": note,
        }

    if native and bitrix_chat_id:
        lead_result = await create_native_openline_lead(
            domain=domain,
            token_enc=tenant.access_token_encrypted,
            bitrix_chat_id=bitrix_chat_id,
            connector_id=connector_id,
            external_chat_id=link.external_chat_id,
            display_name=display_name,
            recipient=recipient,
            first_text=first_text,
        )
    else:
        lead_result = await create_or_get_lead(
            domain=domain,
            token_enc=tenant.access_token_encrypted,
            connector_id=connector_id,
            external_chat_id=link.external_chat_id,
            display_name=display_name,
            recipient=recipient,
            first_text=first_text,
            bitrix_chat_id=bitrix_chat_id,
        )

    if lead_result.get("success") and lead_result.get("id"):
        async with async_session() as session:
            await session.execute(
                sa_update(ChatLink).where(ChatLink.id == chat_link_id).values(
                    crm_entity_type="lead",
                    crm_entity_id=str(lead_result["id"]),
                )
            )
            await session.commit()
        return {
            "success": True,
            "message": "Lead created",
            "id": str(lead_result["id"]),
            "created": lead_result.get("created"),
            "native": lead_result.get("native", False),
            "timeline_note": lead_result.get("timeline_note"),
        }
    return {
        "success": False,
        "message": lead_result.get("error") or "Could not create lead",
        "result": _redact(lead_result),
    }


@router.post("/api/admin/actions/add-openline-note")
async def admin_add_openline_note(request: Request):
    body = await request.json()
    domain = (body.get("domain") or "").strip()
    chat_link_id = int(body.get("chat_link_id") or 0)
    tenant = await _tenant_by_domain(domain) if domain else None
    if not tenant or not chat_link_id:
        return {"success": False, "message": "Missing tenant or chat_link_id"}

    async with async_session() as session:
        link = await session.get(ChatLink, chat_link_id)
        if not link or link.tenant_id != tenant.id:
            return {"success": False, "message": "Dialog not found"}
        first = (await session.execute(
            select(Message).where(Message.chat_link_id == link.id)
            .order_by(Message.created_at.asc()).limit(1)
        )).scalar_one_or_none()
        if not link.crm_entity_id:
            return {"success": False, "message": "Dialog has no lead"}
        result = await ensure_openline_timeline_note(
            domain,
            tenant.access_token_encrypted,
            str(link.crm_entity_id),
            link.bitrix_chat_id,
            display_name=link.external_user_name or link.external_chat_id or "Telegram client",
            recipient=link.external_user_name or link.external_chat_id or "",
            first_text=first.text if first else "",
        )
    return {"success": bool(result.get("success")), "message": "Open Line note checked", "result": result}


@router.get("/api/admin/audit-log")
async def admin_audit_log(limit: int = 200, q: str = ""):
    limit = min(max(limit, 1), 500)
    async with async_session() as session:
        query = select(AdminAuditLog)
        if q:
            like = f"%{q}%"
            query = query.where(or_(
                AdminAuditLog.actor.ilike(like),
                AdminAuditLog.path.ilike(like),
                AdminAuditLog.action.ilike(like),
                AdminAuditLog.details_json.ilike(like),
            ))
        rows = (await session.execute(
            query.order_by(AdminAuditLog.created_at.desc()).limit(limit)
        )).scalars().all()
    return {
        "success": True,
        "logs": [
            {
                "id": row.id,
                "actor": row.actor,
                "ip": row.ip,
                "method": row.method,
                "path": row.path,
                "action": row.action,
                "status_code": row.status_code,
                "details": _redact(_safe_json(row.details_json)),
                "created_at": _dt(row.created_at),
            }
            for row in rows
        ],
    }


@router.get("/api/admin/backups")
async def admin_backups():
    return {
        "success": True,
        "backups": list_backups(),
        "retention_days": settings.backup_retention_days,
    }


@router.post("/api/admin/backups/create")
async def admin_create_backup():
    result = await create_backup_async()
    removed = cleanup_backups()
    return {"success": True, "backup": result, "removed": removed}


@router.post("/api/admin/backups/cleanup")
async def admin_cleanup_backups():
    removed = cleanup_backups()
    return {"success": True, "removed": removed}


@router.get("/api/admin/backups/download/{filename}")
async def admin_download_backup(filename: str):
    safe = os.path.basename(filename)
    if safe != filename or not (safe.endswith(".db") or safe.endswith(".json")):
        return {"success": False, "message": "Invalid filename"}
    path = os.path.join(BACKUP_DIR, safe)
    if not os.path.exists(path):
        return {"success": False, "message": "Backup not found"}
    return FileResponse(path, filename=safe, media_type="application/octet-stream")


@router.get("/api/admin/notifications")
async def admin_notifications():
    return {
        "success": True,
        "configured": bool(
            settings.health_alert_webhook_url or
            (settings.health_alert_telegram_bot_token and settings.health_alert_telegram_chat_id)
        ),
        "webhook": bool(settings.health_alert_webhook_url),
        "telegram": bool(settings.health_alert_telegram_bot_token and settings.health_alert_telegram_chat_id),
        "interval_seconds": settings.health_check_interval_seconds,
    }


@router.post("/api/admin/notifications/test")
async def admin_test_notification():
    text = "SAFY Telegram test notification: admin alerts are connected."
    sent = await send_health_notification(text, {"source": "admin_test"})
    return {
        "success": sent,
        "message": "Notification sent" if sent else "Notifications are not configured",
    }


@router.get("/api/admin/trace/{chat_link_id}")
async def admin_trace(chat_link_id: int, domain: str = ""):
    tenant = await _tenant_by_domain(domain) if domain else None
    async with async_session() as session:
        link = await session.get(ChatLink, chat_link_id)
        if not link or (tenant and link.tenant_id != tenant.id):
            return {"success": False, "message": "Dialog not found"}
        tenant_obj = tenant or await session.get(Tenant, link.tenant_id)
        messages = (await session.execute(
            select(Message)
            .where(Message.chat_link_id == chat_link_id)
            .order_by(Message.created_at.asc())
            .limit(500)
        )).scalars().all()
        message_ids = [m.id for m in messages]
        outbox_rows = []
        if message_ids:
            candidates = (await session.execute(
                select(OutboxEntry).order_by(OutboxEntry.created_at.desc()).limit(1000)
            )).scalars().all()
            for entry in candidates:
                payload = _safe_json(entry.payload_json)
                if payload.get("message_db_id") in message_ids:
                    outbox_rows.append(entry)
        channel = await session.get(Channel, link.channel_id) if link.channel_id else None

    serialized_messages = [_serialize_message(m, link, channel) for m in messages]
    return {
        "success": True,
        "dialog": {
            "id": link.id,
            "client": link.external_user_name or link.external_chat_id,
            "external_chat_id": link.external_chat_id,
            "bitrix_chat_id": link.bitrix_chat_id,
            "bitrix_dialog_id": link.bitrix_dialog_id,
            "crm_entity_type": link.crm_entity_type,
            "crm_entity_id": link.crm_entity_id,
            "lead_url": lead_url(tenant_obj.bitrix_domain, link.crm_entity_id) if tenant_obj and link.crm_entity_id else "",
            "chat_url": openline_chat_url(tenant_obj.bitrix_domain, link.bitrix_chat_id) if tenant_obj and link.bitrix_chat_id else "",
        },
        "messages": serialized_messages,
        "outbox": [_serialize_outbox(e) for e in outbox_rows],
    }


@router.get("/api/admin/messages")
async def admin_messages(
    domain: str,
    limit: int = 150,
    status: str = "",
    direction: str = "",
    chat_link_id: int = 0,
    date_from: str = "",
    date_to: str = "",
    operator: str = "",
    client: str = "",
    q: str = "",
    media_type: str = "",
):
    tenant, messages = await _filtered_admin_messages(
        domain=domain,
        limit=min(max(limit, 1), 500),
        status=status,
        direction=direction,
        chat_link_id=chat_link_id,
        date_from=date_from,
        date_to=date_to,
        operator=operator,
        client=client,
        q=q,
        media_type=media_type,
    )
    if not tenant:
        return {"success": False, "message": "Tenant not found", "messages": []}
    return {"success": True, "messages": messages}


@router.get("/api/admin/messages/export.csv")
async def admin_messages_export(
    domain: str,
    limit: int = 5000,
    status: str = "",
    direction: str = "",
    chat_link_id: int = 0,
    date_from: str = "",
    date_to: str = "",
    operator: str = "",
    client: str = "",
    q: str = "",
    media_type: str = "",
):
    tenant, messages = await _filtered_admin_messages(
        domain=domain,
        limit=limit,
        status=status,
        direction=direction,
        chat_link_id=chat_link_id,
        date_from=date_from,
        date_to=date_to,
        operator=operator,
        client=client,
        q=q,
        media_type=media_type,
    )
    if not tenant:
        return Response("Tenant not found\n", media_type="text/plain", status_code=404)
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=[
        "id", "created_at", "direction", "status", "author", "operator", "target",
        "text", "media_type", "file_count", "telegram_chat_id", "bitrix_chat_id", "crm_entity_id",
        "provider_message_id", "bitrix_message_id", "error",
    ])
    writer.writeheader()
    for item in messages:
        writer.writerow({
            "id": item.get("id"),
            "created_at": item.get("created_at"),
            "direction": item.get("direction"),
            "status": item.get("status"),
            "author": item.get("author"),
            "operator": item.get("operator"),
            "target": item.get("target"),
            "text": item.get("text"),
            "media_type": item.get("media_type"),
            "file_count": item.get("file_count"),
            "telegram_chat_id": item.get("telegram_chat_id"),
            "bitrix_chat_id": item.get("bitrix_chat_id"),
            "crm_entity_id": item.get("crm_entity_id"),
            "provider_message_id": item.get("provider_message_id"),
            "bitrix_message_id": item.get("bitrix_message_id"),
            "error": item.get("error"),
        })
    filename = f"safy-messages-{tenant.bitrix_domain}-{datetime.now(MSK).strftime('%Y%m%d-%H%M')}.csv"
    return Response(
        "\ufeff" + out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/admin/message/{message_id}")
async def admin_message_detail(message_id: int, domain: str = ""):
    tenant = await _tenant_by_domain(domain) if domain else None
    async with async_session() as session:
        query = (
            select(Message, ChatLink, Channel)
            .outerjoin(ChatLink, Message.chat_link_id == ChatLink.id)
            .outerjoin(Channel, Message.channel_id == Channel.id)
            .where(Message.id == message_id)
        )
        if tenant:
            query = query.where(Message.tenant_id == tenant.id)
        result = await session.execute(query)
        row = result.first()
    if not row:
        return {"success": False, "message": "Message not found"}
    message, link, channel = row
    item = _serialize_message(message, link, channel)
    item["raw_payload"] = _redact(_safe_json(message.raw_payload_json))
    return {"success": True, "message": item}


@router.get("/api/admin/operators")
async def admin_operators(domain: str):
    tenant = await _tenant_by_domain(domain)
    if not tenant:
        return {"success": False, "message": "Tenant not found", "operators": []}
    async with async_session() as session:
        rows = (await session.execute(
            select(Message, ChatLink)
            .outerjoin(ChatLink, Message.chat_link_id == ChatLink.id)
            .where(Message.tenant_id == tenant.id, Message.direction == "outbound")
            .order_by(Message.created_at.desc())
        )).all()

    operators = {}
    for message, link in rows:
        name = _operator_name_for_stats(message, link)
        if not name:
            continue
        item = operators.setdefault(name, {"name": name, "total": 0})
        item["total"] += 1

    return {
        "success": True,
        "operators": sorted(operators.values(), key=lambda x: x["name"].lower()),
    }


@router.get("/api/admin/quick-replies")
async def admin_quick_replies(domain: str):
    tenant = await _tenant_by_domain(domain)
    if not tenant:
        return {"success": False, "message": "Tenant not found", "templates": []}
    async with async_session() as session:
        rows = (await session.execute(
            select(QuickReplyTemplate)
            .where(QuickReplyTemplate.tenant_id == tenant.id)
            .order_by(QuickReplyTemplate.sort_order.asc(), QuickReplyTemplate.id.asc())
        )).scalars().all()
    return {"success": True, "templates": [_serialize_quick_reply(item) for item in rows]}


@router.post("/api/admin/quick-replies")
async def admin_save_quick_replies(request: Request):
    body = await request.json()
    domain = (body.get("domain") or "").strip()
    tenant = await _tenant_by_domain(domain) if domain else None
    if not tenant:
        return {"success": False, "message": "Tenant not found"}

    templates = body.get("templates") or []
    cleaned = []
    for idx, item in enumerate(templates):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        title = str(item.get("title") or "").strip() or text.splitlines()[0][:48]
        cleaned.append({
            "title": title[:120],
            "text": text[:4096],
            "sort_order": idx,
            "active": 1 if item.get("active", True) else 0,
        })
        if len(cleaned) >= 35:
            break

    async with async_session() as session:
        await session.execute(sa_delete(QuickReplyTemplate).where(QuickReplyTemplate.tenant_id == tenant.id))
        for item in cleaned:
            session.add(QuickReplyTemplate(tenant_id=tenant.id, **item))
        await session.commit()
        rows = (await session.execute(
            select(QuickReplyTemplate)
            .where(QuickReplyTemplate.tenant_id == tenant.id)
            .order_by(QuickReplyTemplate.sort_order.asc(), QuickReplyTemplate.id.asc())
        )).scalars().all()

    return {
        "success": True,
        "message": f"Saved {len(rows)} quick replies",
        "templates": [_serialize_quick_reply(item) for item in rows],
    }


@router.get("/api/admin/operator-stats")
async def admin_operator_stats(
    domain: str,
    date_from: str = "",
    date_to: str = "",
    operator: str = "",
    client: str = "",
):
    tenant = await _tenant_by_domain(domain)
    if not tenant:
        return {"success": False, "message": "Tenant not found", "days": [], "operators": []}
    start = _parse_date_bound(date_from)
    end = _parse_date_bound(date_to, end=True)
    if not start and not end:
        # Default to today in Moscow time.
        start = _parse_date_bound(datetime.now(MSK).date().isoformat())
        end = _parse_date_bound(datetime.now(MSK).date().isoformat(), end=True)

    async with async_session() as session:
        query = (
            select(Message, ChatLink)
            .outerjoin(ChatLink, Message.chat_link_id == ChatLink.id)
            .where(Message.tenant_id == tenant.id)
        )
        if start:
            query = query.where(Message.created_at >= start)
        if end:
            query = query.where(Message.created_at < end)
        if client:
            like_client = f"%{client}%"
            query = query.where(or_(
                ChatLink.external_user_name.ilike(like_client),
                ChatLink.external_chat_id.ilike(like_client),
                Message.recipient.ilike(like_client),
                Message.chat_id_tg.ilike(like_client),
            ))
        all_rows = (await session.execute(query.order_by(Message.created_at.asc(), Message.id.asc()))).all()

    per_day = {}
    per_operator = {}
    rows = [(message, link) for message, link in all_rows if message.direction == "outbound"]
    for message, link in rows:
        name = _operator_name_for_stats(message, link)
        if not name:
            continue
        if operator and operator.strip().lower() not in name.lower():
            continue
        day = _local_day(message.created_at)
        key = (day, name)
        file_count = len(_file_summary(message.raw_payload_json))
        client_key = (link.external_chat_id if link else None) or message.recipient or message.chat_id_tg or ""

        item = per_day.setdefault(key, {
            "day": day,
            "operator": name,
            "total": 0,
            "sent": 0,
            "failed": 0,
            "files": 0,
            "clients": set(),
            "response_times": [],
        })
        op = per_operator.setdefault(name, {
            "operator": name,
            "total": 0,
            "sent": 0,
            "failed": 0,
            "files": 0,
            "clients": set(),
            "response_times": [],
        })
        for target in (item, op):
            target["total"] += 1
            target["files"] += file_count
            if message.status == "sent":
                target["sent"] += 1
            elif message.status in {"failed", "no_link"}:
                target["failed"] += 1
            if client_key:
                target["clients"].add(client_key)

    first_inbound_by_chat = {}
    counted_response_keys = set()
    for message, link in all_rows:
        chat_key = message.chat_link_id or message.chat_id_tg or (link.external_chat_id if link else None)
        if not chat_key:
            continue
        if message.direction == "inbound" and chat_key not in first_inbound_by_chat:
            first_inbound_by_chat[chat_key] = message.created_at
            continue
        if message.direction != "outbound":
            continue
        name = _operator_name_for_stats(message, link)
        if not name:
            continue
        if operator and operator.strip().lower() not in name.lower():
            continue
        first_inbound_at = first_inbound_by_chat.get(chat_key)
        minutes = _minutes_delta(message.created_at, first_inbound_at)
        response_key = (chat_key, name)
        if minutes is None or response_key in counted_response_keys:
            continue
        counted_response_keys.add(response_key)
        day = _local_day(message.created_at)
        day_item = per_day.get((day, name))
        op_item = per_operator.get(name)
        if day_item is not None:
            day_item["response_times"].append(minutes)
        if op_item is not None:
            op_item["response_times"].append(minutes)

    def finalize(row):
        row = dict(row)
        row["clients"] = len(row["clients"])
        response_times = row.pop("response_times", [])
        row["response_count"] = len(response_times)
        row["avg_response_minutes"] = _average(response_times)
        row["median_response_minutes"] = _median(response_times)
        return row

    return {
        "success": True,
        "days": [finalize(v) for v in sorted(per_day.values(), key=lambda x: (x["day"], x["operator"]), reverse=True)],
        "operators": [finalize(v) for v in sorted(per_operator.values(), key=lambda x: x["total"], reverse=True)],
        "filters": {
            "date_from": date_from,
            "date_to": date_to,
            "operator": operator,
            "client": client,
        },
    }


@router.get("/api/admin/dialogs")
async def admin_dialogs(domain: str, q: str = ""):
    tenant = await _tenant_by_domain(domain)
    if not tenant:
        return {"success": False, "message": "Tenant not found", "dialogs": []}
    async with async_session() as session:
        query = select(ChatLink, Channel).outerjoin(Channel, ChatLink.channel_id == Channel.id).where(ChatLink.tenant_id == tenant.id)
        if q:
            like = f"%{q}%"
            query = query.where(or_(ChatLink.external_user_name.ilike(like), ChatLink.external_chat_id.ilike(like)))
        result = await session.execute(query.order_by(ChatLink.updated_at.desc()))
        rows = result.all()
        dialogs = []
        seen_dialogs = set()
        for link, channel in rows:
            dialog_key = (link.provider, link.account_id or 0, link.external_chat_id or f"link:{link.id}")
            if dialog_key in seen_dialogs:
                continue
            seen_dialogs.add(dialog_key)
            same_link_ids = [link.id]
            if link.external_chat_id:
                same_link_query = select(ChatLink.id).where(
                    ChatLink.tenant_id == tenant.id,
                    ChatLink.provider == link.provider,
                    ChatLink.external_chat_id == link.external_chat_id,
                )
                if link.account_id is None:
                    same_link_query = same_link_query.where(ChatLink.account_id.is_(None))
                    chat_id_scope = and_(Message.chat_id_tg == link.external_chat_id, Message.account_id.is_(None))
                else:
                    same_link_query = same_link_query.where(ChatLink.account_id == link.account_id)
                    chat_id_scope = and_(Message.chat_id_tg == link.external_chat_id, Message.account_id == link.account_id)
                same_link_ids = [
                    row_id for (row_id,) in (await session.execute(same_link_query)).all()
                ] or [link.id]
            else:
                chat_id_scope = None
            message_scope = [Message.chat_link_id.in_(same_link_ids)]
            if chat_id_scope is not None:
                message_scope.append(chat_id_scope)
            count = (await session.execute(
                select(func.count(func.distinct(Message.id))).where(
                    Message.tenant_id == tenant.id,
                    or_(*message_scope),
                )
            )).scalar_one()
            latest = (await session.execute(
                select(Message).where(
                    Message.tenant_id == tenant.id,
                    or_(*message_scope),
                ).order_by(Message.created_at.desc()).limit(1)
            )).scalar_one_or_none()
            last_outbound_at = (await session.execute(
                select(func.max(Message.created_at)).where(
                    Message.tenant_id == tenant.id,
                    Message.direction == "outbound",
                    or_(*message_scope),
                )
            )).scalar_one()
            unread_conditions = [
                Message.tenant_id == tenant.id,
                Message.direction == "inbound",
                or_(*message_scope),
            ]
            if last_outbound_at:
                unread_conditions.append(Message.created_at > last_outbound_at)
            unanswered_count = (await session.execute(
                select(func.count(func.distinct(Message.id))).where(*unread_conditions)
            )).scalar_one()
            oldest_unanswered_at = (await session.execute(
                select(func.min(Message.created_at)).where(*unread_conditions)
            )).scalar_one() if unanswered_count else None
            unanswered_wait_minutes = 0
            if oldest_unanswered_at:
                unanswered_wait_minutes = max(0, int((_naive_utc(utcnow()) - _naive_utc(oldest_unanswered_at)).total_seconds() // 60))
            needs_reply = bool(latest and latest.direction == "inbound" and unanswered_count)
            dialogs.append({
                "id": link.id,
                "name": link.external_user_name or link.external_chat_id,
                "external_chat_id": link.external_chat_id,
                "external_user_id": link.external_user_id,
                "provider": link.provider,
                "bitrix_line_id": link.bitrix_line_id,
                "bitrix_chat_id": link.bitrix_chat_id,
                "bitrix_dialog_id": link.bitrix_dialog_id,
                "crm_entity_type": link.crm_entity_type,
                "crm_entity_id": link.crm_entity_id,
                "channel_id": link.channel_id,
                "channel_name": channel.name if channel else None,
                "message_count": count,
                "unanswered_count": unanswered_count,
                "needs_reply": needs_reply,
                "unanswered_since_at": _dt(oldest_unanswered_at),
                "unanswered_wait_minutes": unanswered_wait_minutes,
                "long_wait": unanswered_wait_minutes >= 30,
                "latest_text": latest.text if latest else "",
                "latest_status": latest.status if latest else "",
                "latest_direction": latest.direction if latest else "",
                "latest_media_type": _message_media_kind(latest),
                "last_message_at": _dt(link.last_message_at or (latest.created_at if latest else None)),
                "updated_at": _dt(link.updated_at),
            })
    return {"success": True, "dialogs": dialogs}


@router.get("/api/admin/outbox")
async def admin_outbox(status: str = "", limit: int = 100):
    limit = min(max(limit, 1), 300)
    async with async_session() as session:
        query = select(OutboxEntry)
        if status:
            query = query.where(OutboxEntry.status == status)
        result = await session.execute(query.order_by(OutboxEntry.created_at.desc()).limit(limit))
        entries = result.scalars().all()
        counts = dict((await session.execute(
            select(OutboxEntry.status, func.count(OutboxEntry.id)).group_by(OutboxEntry.status)
        )).all())
    return {
        "success": True,
        "counts": counts,
        "entries": [_serialize_outbox(e) for e in entries],
    }


@router.get("/api/admin/errors")
async def admin_errors(domain: str = "", limit: int = 100):
    limit = min(max(limit, 1), 300)
    tenant = await _tenant_by_domain(domain) if domain else None
    async with async_session() as session:
        msg_query = (
            select(Message, ChatLink, Channel)
            .outerjoin(ChatLink, Message.chat_link_id == ChatLink.id)
            .outerjoin(Channel, Message.channel_id == Channel.id)
            .where(Message.status.in_(["failed", "no_link"]))
        )
        if tenant:
            msg_query = msg_query.where(Message.tenant_id == tenant.id)
        messages = (await session.execute(
            msg_query.order_by(Message.created_at.desc()).limit(limit)
        )).all()
        outbox = (await session.execute(
            select(OutboxEntry).where(OutboxEntry.status == "failed")
            .order_by(OutboxEntry.updated_at.desc()).limit(limit)
        )).scalars().all()
    return {
        "success": True,
        "messages": [_serialize_message(m, l, c) for m, l, c in messages],
        "outbox": [_serialize_outbox(e) for e in outbox],
    }


@router.get("/api/admin/accounts")
async def admin_accounts(domain: str):
    tenant = await _tenant_by_domain(domain)
    if not tenant:
        return {"success": False, "message": "Tenant not found", "accounts": []}
    try:
        from app.telegram.service import _active_clients
        active_ids = set(_active_clients.keys())
    except Exception:
        active_ids = set()
    async with async_session() as session:
        result = await session.execute(
            select(TelegramAccount).where(TelegramAccount.tenant_id == tenant.id).order_by(TelegramAccount.id.desc())
        )
        accounts = result.scalars().all()
    return {
        "success": True,
        "accounts": [
            {
                "id": a.id,
                "api_id": a.api_id,
                "phone": a.phone,
                "username": a.username,
                "bitrix_line_id": a.bitrix_line_id,
                "bitrix_connector_id": a.bitrix_connector_id,
                "state": a.state,
                "is_listening": a.id in active_ids,
                "created_at": _dt(a.created_at),
                "updated_at": _dt(a.updated_at),
            }
            for a in accounts
        ],
    }


@router.get("/api/tenant/{domain}")
async def get_tenant(domain: str):
    async with async_session() as session:
        result = await session.execute(select(Tenant).where(Tenant.bitrix_domain == domain))
        tenant = result.scalar_one_or_none()
        if not tenant:
            return {"success": False, "message": "Tenant not found"}
        return {
            "success": True,
            "domain": tenant.bitrix_domain,
            "status": tenant.status,
            "member_id": tenant.member_id,
            "install_user_id": tenant.install_user_id,
        }


@router.post("/api/activate-line")
async def activate_line(request: Request):
    body = await request.json()
    domain = body.get("domain")
    line_id = int(body.get("line_id", 0))

    if not domain or not line_id:
        return {"success": False, "message": "Missing domain or line_id"}

    async with async_session() as session:
        result = await session.execute(select(Tenant).where(Tenant.bitrix_domain == domain))
        tenant = result.scalar_one_or_none()
        if not tenant:
            return {"success": False, "message": "Tenant not found"}

        try:
            await bx.activate_connector(
                domain, tenant.access_token_encrypted,
                settings.connector_id, line_id, 1,
            )
            data_id = f"{settings.connector_id}_line_{line_id}"
            await bx.set_connector_data(
                domain, tenant.access_token_encrypted,
                settings.connector_id, line_id,
                {
                    "ID": data_id,
                    "NAME": settings.connector_name,
                    "URL": f"{settings.app_url}/bitrix/install",
                    "URL_IM": f"{settings.app_url}/bitrix/widget",
                },
            )
        except Exception as e:
            logger.warning(f"Bitrix24 API failed during activate-line: {e}")

        existing_ch = await session.execute(
            select(Channel).where(Channel.tenant_id == tenant.id,
                                  Channel.bitrix_line_id == line_id)
        )
        if not existing_ch.scalar_one_or_none():
            ch = Channel(
                tenant_id=tenant.id, provider=settings.connector_id,
                name=f"Line {line_id}", bitrix_line_id=line_id, status="active",
            )
            session.add(ch)
            await session.commit()

    return {"success": True, "message": f"Line {line_id} activated"}
