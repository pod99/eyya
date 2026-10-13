import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, or_, select, update

from app.api.routes import (
    handle_operator_reply,
    _parse_bitrix_text,
    _primary_connector_id,
    _primary_line_id,
    _telegram_account_for_link,
)
from app.config import settings
from app.crypto import decrypt_token
from app.database import async_session, ChatLink, Message, Tenant, utcnow

logger = logging.getLogger(__name__)
_USER_NAME_CACHE: dict[tuple[str, str], tuple[str, float]] = {}
BASE_POLL_INTERVAL_SECONDS = 10
MAX_BACKOFF_SECONDS = 300
POLL_RECENT_WINDOW_SECONDS = 72 * 3600
MAX_LINKS_PER_POLL = 150


class RetryablePollerError(RuntimeError):
    pass


def _is_retryable_error(error: str) -> bool:
    text = (error or "").lower()
    return any(marker in text for marker in (
        "query_limit_exceeded",
        "too many requests",
        "rate limit",
        "timeout",
        "temporarily",
        "429",
        "500",
        "502",
        "503",
        "504",
    ))


async def _bitrix_call(domain: str, token: str, method: str, payload: dict) -> dict:
    request_payload = dict(payload or {})
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"https://{domain}/rest/{method}",
            headers={"Authorization": f"Bearer {token}"},
            json=request_payload,
        )
    if response.status_code in {429, 500, 502, 503, 504}:
        raise RetryablePollerError(f"Bitrix HTTP {response.status_code} for {method}")
    data = response.json()
    if data.get("error"):
        message = data.get("error_description") or data.get("error")
        if _is_retryable_error(message):
            raise RetryablePollerError(message)
        raise RuntimeError(message)
    return data.get("result") or {}


async def _file_entries(domain: str, token: str, file_ids) -> list[dict]:
    entries = []
    if not isinstance(file_ids, list):
        file_ids = [file_ids]
    for file_id in file_ids[:5]:
        try:
            info = await _bitrix_call(domain, token, "disk.file.get", {"id": int(file_id)})
        except Exception as exc:
            logger.warning(f"OpenLine poller: disk.file.get failed for {file_id}: {exc}")
            continue
        download_url = info.get("DOWNLOAD_URL") or info.get("downloadUrl") or ""
        name = info.get("NAME") or f"file_{file_id}"
        if download_url:
            entries.append({"name": name, "downloadLink": download_url, "DOWNLOAD_URL": download_url})
    return entries


def _normalize_history_messages(history) -> list[dict]:
    if not isinstance(history, dict):
        return []
    messages = history.get("message")
    if messages is None:
        messages = history.get("messages")
    if messages is None and isinstance(history.get("result"), dict):
        messages = history["result"].get("message") or history["result"].get("messages")
    if isinstance(messages, dict):
        result = []
        for key, msg in messages.items():
            if isinstance(msg, dict):
                item = dict(msg)
                item.setdefault("id", key)
                result.append(item)
        return result
    if isinstance(messages, list):
        return [msg for msg in messages if isinstance(msg, dict)]
    return []


def _message_params(msg: dict) -> dict:
    params = msg.get("params") or msg.get("PARAMS") or {}
    return params if isinstance(params, dict) else {}


def _message_text(msg: dict) -> str:
    text = msg.get("text")
    if text is None:
        text = msg.get("TEXT")
    if text is None and isinstance(msg.get("message"), dict):
        text = msg["message"].get("text") or msg["message"].get("TEXT")
    return text or ""


def _message_sender_id(msg: dict):
    return msg.get("senderid") or msg.get("senderId") or msg.get("SENDER_ID") or msg.get("author_id") or msg.get("AUTHOR_ID") or ""


def _param_value(params: dict, *names):
    lowered = {str(k).lower(): v for k, v in params.items()}
    for name in names:
        key = str(name).lower()
        if key in lowered:
            return lowered[key]
    return None


async def _dialog_history(domain: str, token: str, chat_id: int) -> dict:
    try:
        return await _bitrix_call(
            domain,
            token,
            "imopenlines.session.history.get",
            {"CHAT_ID": chat_id},
        )
    except Exception as exc:
        if isinstance(exc, RetryablePollerError):
            raise
        logger.warning(f"OpenLine poller: history.get failed for chat {chat_id}: {exc}")

    for payload in (
        {"DIALOG_ID": f"chat{chat_id}", "LIMIT": 50},
        {"DIALOG_ID": str(chat_id), "LIMIT": 50},
    ):
        try:
            return await _bitrix_call(domain, token, "im.dialog.messages.get", payload)
        except Exception as exc:
            if isinstance(exc, RetryablePollerError):
                raise
            logger.warning(
                "OpenLine poller: im.dialog.messages.get failed "
                f"for chat {chat_id} payload={payload}: {exc}"
            )
    return {}


async def _bitrix_user_name(domain: str, token: str, user_id) -> str:
    user_id = str(user_id or "")
    if not user_id or user_id == "0":
        return "Bitrix operator"
    cache_key = (domain, user_id)
    now = time.time()
    cached = _USER_NAME_CACHE.get(cache_key)
    if cached and now - cached[1] < int(settings.cache_ttl_seconds or 3600):
        return cached[0]
    if len(_USER_NAME_CACHE) > int(settings.cache_max_size or 1000):
        expired_keys = [
            key for key, (_, created_at) in _USER_NAME_CACHE.items()
            if now - created_at >= int(settings.cache_ttl_seconds or 3600)
        ]
        for key in expired_keys:
            _USER_NAME_CACHE.pop(key, None)
        overflow = max(0, len(_USER_NAME_CACHE) - int(settings.cache_max_size or 1000))
        if overflow:
            for key, _ in sorted(_USER_NAME_CACHE.items(), key=lambda item: item[1][1])[:overflow]:
                _USER_NAME_CACHE.pop(key, None)
    fallback = f"Bitrix user #{user_id}"
    try:
        result = await _bitrix_call(domain, token, "user.get", {"ID": int(user_id)})
        user = result[0] if isinstance(result, list) and result else {}
        name = " ".join(
            part for part in [
                user.get("NAME") or user.get("FIRST_NAME"),
                user.get("LAST_NAME"),
            ]
            if part
        ).strip()
        if not name:
            name = user.get("EMAIL") or fallback
    except Exception as exc:
        logger.warning(f"OpenLine poller: user.get failed for {user_id}: {exc}")
        name = fallback
    _USER_NAME_CACHE[cache_key] = (name, now)
    return name


async def _already_seen(domain: str, external_chat_id: str, bitrix_message_id: str) -> bool:
    idempotency_key = f"bitrix_{domain}_{external_chat_id}_{bitrix_message_id}"
    async with async_session() as session:
        result = await session.execute(select(Message.id).where(Message.idempotency_key == idempotency_key))
        return result.scalar_one_or_none() is not None


async def _sync_existing_edit(tenant: Tenant, link: ChatLink, msg: dict, connector_id: str) -> bool:
    bitrix_message_id = str(msg.get("id") or "")
    if not bitrix_message_id:
        return False

    _, clean_text = _parse_bitrix_text(_message_text(msg))
    if not clean_text:
        return False

    async with async_session() as session:
        existing = (await session.execute(
            select(Message).where(
                Message.chat_link_id == link.id,
                Message.bitrix_message_id == bitrix_message_id,
                Message.provider_message_id.isnot(None),
                or_(Message.sender_name.is_(None), Message.sender_name != "Bitrix edit"),
            ).order_by(Message.id.asc()).limit(1)
        )).scalars().first()
        if not existing or (existing.text or "") == clean_text:
            return False

        tg_account = await _telegram_account_for_link(session, tenant.id, link)
        if not tg_account:
            logger.warning(f"OpenLine poller: edit skipped, no Telegram account for link={link.id}")
            return False

        provider_ids = [mid for mid in (existing.provider_message_id or "").split(",") if mid]
        if not provider_ids:
            return False
        recent_recipient = (await session.execute(
            select(Message.recipient).where(
                Message.chat_link_id == link.id,
                Message.recipient.isnot(None),
                Message.recipient.like("@%"),
            ).order_by(Message.id.desc()).limit(1)
        )).scalar_one_or_none()
        name_match = re.search(r"\(@([A-Za-z0-9_]{3,})\)", link.external_user_name or "")
        chat_target = recent_recipient or (f"@{name_match.group(1)}" if name_match else None)
        if not chat_target:
            try:
                chat_target = int(link.external_chat_id)
            except (TypeError, ValueError):
                chat_target = link.external_chat_id
        message_db_id = existing.id
        old_text = existing.text
        account_id = tg_account.id

    from app.telegram.service import edit_message
    edited_any = False
    last_error = None
    for provider_id in provider_ids[:3]:
        result = await edit_message(account_id, chat_target, provider_id, clean_text)
        if result.get("success"):
            edited_any = True
        else:
            last_error = result.get("error")

    text_hash = hashlib.md5(clean_text.encode()).hexdigest()[:12]
    event_key = f"bitrix_poll_edit_{tenant.bitrix_domain}_{link.external_chat_id}_{bitrix_message_id}_{text_hash}"
    async with async_session() as session:
        status = "edited" if edited_any else "edit_failed"
        await session.execute(
            update(Message).where(Message.id == message_db_id).values(
                text=clean_text if edited_any else old_text,
                status=status,
                error=None if edited_any else last_error,
            )
        )
        existing_event = await session.execute(select(Message.id).where(Message.idempotency_key == event_key))
        event_row_id = existing_event.scalar_one_or_none()
        if event_row_id:
            await session.execute(
                update(Message).where(Message.id == event_row_id).values(
                    text=clean_text,
                    status=status,
                    error=None if edited_any else last_error,
                    updated_at=utcnow(),
                )
            )
        else:
            session.add(Message(
                tenant_id=tenant.id,
                channel_id=link.channel_id,
                chat_link_id=link.id,
                direction="outbound",
                bitrix_message_id=bitrix_message_id,
                provider_message_id=",".join(provider_ids),
                text=clean_text,
                status=status,
                error=None if edited_any else last_error,
                raw_payload_json=json.dumps({"source": "openline_poller_edit", "history": msg}, ensure_ascii=False),
                idempotency_key=event_key,
                account_id=account_id,
                chat_id_tg=link.external_chat_id,
                sender_name="Bitrix edit",
            ))
        await session.commit()

    if edited_any:
        logger.info(
            "OpenLine poller: edited Telegram message(s) %s for Bitrix message %s",
            provider_ids, bitrix_message_id,
        )
    return edited_any


async def _poll_once(started_at: int):
    connector_id = _primary_connector_id()
    line_id = _primary_line_id()
    recent_cutoff = utcnow() - timedelta(seconds=POLL_RECENT_WINDOW_SECONDS)

    async with async_session() as session:
        latest_messages = (
            select(
                Message.chat_link_id.label("chat_link_id"),
                func.max(Message.created_at).label("latest_at"),
            )
            .where(Message.chat_link_id.isnot(None))
            .group_by(Message.chat_link_id)
            .subquery()
        )
        result = await session.execute(
            select(Tenant, ChatLink)
            .join(ChatLink, ChatLink.tenant_id == Tenant.id)
            .outerjoin(latest_messages, latest_messages.c.chat_link_id == ChatLink.id)
            .where(
                ChatLink.provider == connector_id,
                ChatLink.bitrix_line_id == line_id,
                ChatLink.bitrix_chat_id.is_not(None),
                or_(
                    ChatLink.last_message_at >= recent_cutoff,
                    ChatLink.updated_at >= recent_cutoff,
                    latest_messages.c.latest_at >= recent_cutoff,
                ),
            )
            .order_by(func.coalesce(latest_messages.c.latest_at, ChatLink.last_message_at, ChatLink.updated_at).desc())
            .limit(MAX_LINKS_PER_POLL)
        )
        rows = result.all()

    for tenant, link in rows:
        if not link.external_chat_id or not link.bitrix_chat_id:
            continue
        try:
            chat_id = int(link.bitrix_chat_id)
        except (TypeError, ValueError):
            continue

        token = decrypt_token(tenant.access_token_encrypted)
        history = await _dialog_history(tenant.bitrix_domain, token, chat_id)
        if not history:
            continue

        messages = _normalize_history_messages(history)
        for msg in sorted(messages, key=lambda item: int(item.get("id") or 0), reverse=True):
            params = _message_params(msg)
            await _sync_existing_edit(tenant, link, msg, connector_id)
            if _param_value(params, "sending") != "Y":
                continue

            bitrix_message_id = str(msg.get("id") or "")
            if not bitrix_message_id:
                continue
            if await _already_seen(tenant.bitrix_domain, link.external_chat_id, bitrix_message_id):
                continue

            files = await _file_entries(tenant.bitrix_domain, token, _param_value(params, "fileId", "file_id") or [])
            text = _message_text(msg)
            sender_id = _message_sender_id(msg)
            sender_name = await _bitrix_user_name(tenant.bitrix_domain, token, sender_id)
            data = {
                "CONNECTOR": connector_id,
                "LINE": line_id,
                "MESSAGES": [{
                    "user": {
                        "id": str(sender_id),
                        "name": sender_name,
                    },
                    "im": {"chat_id": chat_id, "message_id": bitrix_message_id},
                    "message": {"id": [bitrix_message_id], "text": text, "files": files},
                    "chat": {
                        "id": link.external_chat_id,
                        "name": link.external_user_name or link.external_chat_id,
                    },
                }],
            }
            logger.info(
                "OpenLine poller: handling pending operator message "
                f"{bitrix_message_id} chat={chat_id} files={len(files)}"
            )
            await handle_operator_reply(tenant.bitrix_domain, data, connector_id)


async def openline_pending_poller():
    started_at = int(time.time())
    delay = BASE_POLL_INTERVAL_SECONDS
    failures = 0
    await asyncio.sleep(BASE_POLL_INTERVAL_SECONDS)
    while True:
        try:
            await _poll_once(started_at)
            failures = 0
            delay = BASE_POLL_INTERVAL_SECONDS
        except Exception as exc:
            failures += 1
            delay = min(MAX_BACKOFF_SECONDS, BASE_POLL_INTERVAL_SECONDS * (2 ** min(failures - 1, 5)))
            logger.error("OpenLine poller error: %s; next retry in %ss", exc, delay)
        await asyncio.sleep(delay)
