import asyncio
import logging
import os
import time
from io import BytesIO
import telethon
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession
from telethon.tl.types import (
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
    MessageMediaContact,
    MessageMediaGeo,
    MessageMediaGeoLive,
)
from app.config import settings

logger = logging.getLogger(__name__)

_active_clients = {}
_listener_tasks = {}
_listener_restart_attempts = {}
_stopping_clients = set()
_pending_logins = {}
_suppressed_outgoing = {}
_grouped_media_buffers = {}

PROXY = ("socks5", settings.proxy_host, int(settings.proxy_port)) if settings.proxy_host and settings.proxy_port else None


class PermanentTelegramSessionError(Exception):
    pass


def _is_permanent_session_error(exc: Exception) -> bool:
    name = exc.__class__.__name__
    text = str(exc).lower()
    permanent_names = {
        "AuthKeyDuplicatedError",
        "AuthKeyUnregisteredError",
        "AuthKeyInvalidError",
        "SessionRevokedError",
        "UnauthorizedError",
        "UserDeactivatedError",
    }
    if name in permanent_names:
        return True
    return (
        "authorization key" in text
        or "session file" in text
        or "not authorized" in text
        or "please enter your phone" in text
    )


async def _mark_account_failed(account_id: int, reason: str):
    try:
        from sqlalchemy import update
        from app.database import async_session, TelegramAccount, utcnow

        async with async_session() as session:
            await session.execute(
                update(TelegramAccount)
                .where(TelegramAccount.id == account_id)
                .values(state="failed", updated_at=utcnow())
            )
            await session.commit()
        logger.error("Telegram account %s marked failed: %s", account_id, reason)
    except Exception:
        logger.exception("Failed to mark Telegram account %s as failed", account_id)


def _pending_login_ttl() -> int:
    return max(60, int(settings.pending_login_ttl_seconds or 600))


def _cache_ttl_seconds(default: int = 3600) -> int:
    return max(60, int(settings.cache_ttl_seconds or default))


async def _cleanup_pending_logins():
    now = time.time()
    expired = [
        key for key, value in _pending_logins.items()
        if now - float(value.get("created_at", now)) > _pending_login_ttl()
    ]
    overflow = max(0, len(_pending_logins) - int(settings.cache_max_size or 1000))
    if overflow:
        oldest = sorted(_pending_logins.items(), key=lambda item: item[1].get("created_at", 0))[:overflow]
        expired.extend(key for key, _ in oldest)

    for key in set(expired):
        login_data = _pending_logins.pop(key, None)
        client = login_data.get("client") if isinstance(login_data, dict) else None
        if client:
            try:
                await client.disconnect()
            except Exception:
                logger.debug("Failed to disconnect expired Telegram login client", exc_info=True)


def _suppress_outgoing_event(account_id: int, chat_id, message_id):
    if chat_id is None or message_id is None:
        return
    _cleanup_suppressed_outgoing(account_id)
    bucket = _suppressed_outgoing.setdefault(account_id, {})
    cache_limit = max(10, int(settings.cache_max_size or 1000))
    if len(bucket) > cache_limit:
        for key, _ in sorted(bucket.items(), key=lambda item: item[1])[: len(bucket) - cache_limit]:
            bucket.pop(key, None)
    bucket[(str(chat_id), str(message_id))] = time.time()


def _is_suppressed_outgoing(account_id: int, chat_id, message_id) -> bool:
    _cleanup_suppressed_outgoing(account_id)
    bucket = _suppressed_outgoing.get(account_id)
    if not bucket:
        return False
    key = (str(chat_id), str(message_id))
    return bucket.pop(key, None) is not None


def _cleanup_suppressed_outgoing(account_id: int | None = None):
    now = time.time()
    ttl = min(_cache_ttl_seconds(), 3600)
    account_ids = [account_id] if account_id is not None else list(_suppressed_outgoing.keys())
    for acc_id in account_ids:
        bucket = _suppressed_outgoing.get(acc_id)
        if not bucket:
            continue
        for key, created_at in list(bucket.items()):
            if now - created_at > ttl:
                bucket.pop(key, None)
        if not bucket:
            _suppressed_outgoing.pop(acc_id, None)


def _cleanup_grouped_media_buffers():
    now = time.time()
    ttl = min(_cache_ttl_seconds(), 60)
    for key, bucket in list(_grouped_media_buffers.items()):
        if now - float(bucket.get("created_at", now)) <= ttl:
            continue
        task = bucket.get("task")
        if task and not task.done():
            task.cancel()
        _grouped_media_buffers.pop(key, None)


def _message_peer_id(message):
    peer = getattr(message, "peer_id", None)
    if peer is not None:
        try:
            return telethon.utils.get_peer_id(peer)
        except Exception:
            pass
    return (
        getattr(peer, "user_id", None) or
        getattr(peer, "channel_id", None) or
        getattr(peer, "chat_id", None)
    )


def _is_entity_not_found_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return "could not find the input entity" in text or "cannot find any entity" in text


def _is_photo_filename(filename: str) -> bool:
    return os.path.splitext(filename or "")[1].lower() in {".jpg", ".jpeg", ".png", ".webp"}


async def _resolve_dialog_entity(client, recipient):
    try:
        target_id = int(recipient)
    except (TypeError, ValueError):
        return None
    try:
        async for dialog in client.iter_dialogs(limit=500):
            entity = getattr(dialog, "entity", None)
            if not entity:
                continue
            entity_ids = {getattr(entity, "id", None)}
            try:
                entity_ids.add(telethon.utils.get_peer_id(entity))
            except Exception:
                pass
            if target_id in entity_ids or str(target_id) in {str(item) for item in entity_ids if item is not None}:
                return entity
    except Exception:
        logger.debug("Failed to warm Telegram dialog entity cache", exc_info=True)
    return None


def _get_media_name(message) -> tuple[str, str]:
    """Returns (ext, friendly_name) for message media."""
    if message.photo:
        return ".jpg", f"photo_{message.id}.jpg"
    doc = getattr(message, "document", None)
    if doc:
        for attr in doc.attributes:
            if isinstance(attr, DocumentAttributeFilename):
                ext = os.path.splitext(attr.file_name)[1] or ".bin"
                return ext, attr.file_name
        mime = getattr(doc, "mime_type", "") or ""
        if mime.startswith("image/"):
            ext = "." + mime.split("/")[1]
            return ext, f"image_{message.id}{ext}"
        if mime.startswith("video/"):
            ext = "." + mime.split("/")[1]
            return ext, f"video_{message.id}{ext}"
        if "audio" in mime or "ogg" in mime:
            return ".ogg", f"audio_{message.id}.ogg"
        return ".bin", f"file_{message.id}.bin"
    return "", ""


def _format_duration(seconds) -> str:
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        total = 0
    if total <= 0:
        return ""
    minutes, sec = divmod(total, 60)
    if minutes:
        return f"~{minutes} мин {sec:02d} сек"
    return f"~{sec} сек"


def _document_attributes(message):
    doc = getattr(message, "document", None)
    return list(getattr(doc, "attributes", []) or [])


def _sticker_emoji(sticker) -> str:
    for attr in getattr(sticker, "attributes", []) or []:
        if isinstance(attr, DocumentAttributeSticker) and getattr(attr, "alt", None):
            return attr.alt
    return "🎭"


def _sticker_media_name(message, mime: str) -> str:
    if "webp" in mime:
        return f"sticker_{message.id}.webp"
    if "webm" in mime:
        return f"sticker_{message.id}.webm"
    return f"sticker_{message.id}.bin"


def _media_prefix(message) -> str:
    for attr in _document_attributes(message):
        if isinstance(attr, DocumentAttributeAudio) and getattr(attr, "voice", False):
            duration = _format_duration(getattr(attr, "duration", 0))
            return f"[Голосовое сообщение{', ' + duration if duration else ''}]"
        if isinstance(attr, DocumentAttributeVideo) and getattr(attr, "round_message", False):
            duration = _format_duration(getattr(attr, "duration", 0))
            return f"[Видео-сообщение{', ' + duration if duration else ''}]"
    return ""


def _special_media_text(message) -> str:
    media = getattr(message, "media", None)
    if isinstance(media, MessageMediaContact):
        name = " ".join(part for part in [getattr(media, "first_name", ""), getattr(media, "last_name", "")] if part).strip()
        phone = getattr(media, "phone_number", "") or ""
        return f"[Контакт: {name or 'без имени'}{', ' + phone if phone else ''}]"
    if isinstance(media, (MessageMediaGeo, MessageMediaGeoLive)):
        geo = getattr(media, "geo", None)
        lat = getattr(geo, "lat", None)
        lon = getattr(geo, "long", None)
        if lat is not None and lon is not None:
            return f"[Геопозиция: {lat}, {lon}] https://maps.google.com/?q={lat},{lon}"
        return "[Геопозиция]"
    return ""


async def _forward_prefix(client, message) -> str:
    fwd = getattr(message, "fwd_from", None)
    if not fwd:
        return ""
    name = (
        getattr(fwd, "from_name", None) or
        getattr(fwd, "post_author", None) or
        getattr(fwd, "saved_from_name", None) or
        ""
    )
    from_id = getattr(fwd, "from_id", None)
    if not name and from_id:
        try:
            entity = await client.get_entity(from_id)
            name = " ".join(
                part for part in [getattr(entity, "first_name", ""), getattr(entity, "last_name", "")]
                if part
            ).strip() or getattr(entity, "title", "") or getattr(entity, "username", "")
        except Exception:
            name = ""
    return f"[Переслано от: {name or 'неизвестно'}]"


def _append_unique(lines: list[str], text: str):
    text = (text or "").strip()
    if text and text not in lines:
        lines.append(text)


async def _download_media_file(client, message, media_name: str | None = None) -> dict | None:
    media_size = _media_size(message)
    _, fallback_name = _get_media_name(message)
    file_name = media_name or fallback_name or f"media_{message.id}.bin"
    max_bytes = int(settings.max_telegram_media_bytes or 0)
    if max_bytes and media_size and media_size > max_bytes:
        logger.warning(
            "Telegram media skipped for msg %s: %s bytes exceeds limit %s",
            message.id, media_size, max_bytes,
        )
        return {"too_large": True, "name": file_name, "size": media_size}
    try:
        buf = BytesIO()
        await client.download_media(message, file=buf)
        media_bytes = buf.getvalue()
        if max_bytes and len(media_bytes) > max_bytes:
            logger.warning(
                "Telegram media discarded for msg %s after download: %s bytes exceeds limit %s",
                message.id, len(media_bytes), max_bytes,
            )
            return {"too_large": True, "name": file_name, "size": len(media_bytes)}
        return {"bytes": media_bytes, "name": file_name}
    except Exception as e:
        logger.warning(f"Failed to download media for msg {message.id}: {e}")
        return None


async def _extract_message_content(client, message) -> dict:
    lines = []
    media_files = []

    _append_unique(lines, await _forward_prefix(client, message))
    _append_unique(lines, getattr(message, "text", "") or getattr(message, "message", ""))
    _append_unique(lines, _special_media_text(message))

    sticker = getattr(message, "sticker", None)
    if sticker:
        emoji = _sticker_emoji(sticker)
        mime = getattr(sticker, "mime_type", "") or getattr(getattr(message, "document", None), "mime_type", "") or ""
        _append_unique(lines, f"[Стикер {emoji}]")
        if mime == "application/x-tgsticker":
            return {"text": "\n".join(lines), "media_files": media_files}
        downloaded = await _download_media_file(client, message, _sticker_media_name(message, mime))
        if downloaded and downloaded.get("bytes"):
            media_files.append({"bytes": downloaded["bytes"], "name": downloaded["name"]})
        elif downloaded and downloaded.get("too_large"):
            _append_unique(lines, f"[Файл слишком большой: {downloaded['name']}, {downloaded['size']} bytes]")
        return {"text": "\n".join(lines), "media_files": media_files}

    media_prefix = _media_prefix(message)
    if media_prefix:
        lines.insert(0, media_prefix)

    if getattr(message, "photo", None) or getattr(message, "document", None):
        downloaded = await _download_media_file(client, message)
        if downloaded and downloaded.get("bytes"):
            media_files.append({"bytes": downloaded["bytes"], "name": downloaded["name"]})
        elif downloaded and downloaded.get("too_large"):
            _append_unique(lines, f"[Файл слишком большой: {downloaded['name']}, {downloaded['size']} bytes]")

    return {"text": "\n".join(line for line in lines if line), "media_files": media_files}


def _media_size(message) -> int:
    file_obj = getattr(message, "file", None)
    if file_obj and getattr(file_obj, "size", None):
        return int(file_obj.size)
    doc = getattr(message, "document", None)
    if doc and getattr(doc, "size", None):
        return int(doc.size)
    return 0


def _pending_login_allowed(login_data: dict, account_id: int | None = None, domain: str = "") -> bool:
    if not account_id:
        return True
    try:
        pending_account_id = int(login_data.get("account_id") or 0)
    except (TypeError, ValueError):
        pending_account_id = 0
    if pending_account_id != int(account_id):
        return False
    pending_domain = str(login_data.get("domain") or "").strip()
    requested_domain = str(domain or "").strip()
    return not requested_domain or pending_domain == requested_domain


def _login_mismatch_response() -> dict:
    return {"status": "expired", "error": "Login session does not match account"}


async def start_qr_login(api_id: int, api_hash: str, login_key: str, account_id: int | None = None, domain: str = ""):
    await _cleanup_pending_logins()
    client = TelegramClient(StringSession(), api_id, api_hash, proxy=PROXY)
    await client.connect()
    qr_login = await client.qr_login()
    session_string = client.session.save()

    _pending_logins[login_key] = {
        "client": client,
        "api_id": api_id,
        "api_hash": api_hash,
        "qr_url": qr_login.url,
        "session_string": session_string,
        "created_at": time.time(),
        "account_id": account_id,
        "domain": str(domain or ""),
    }

    return {"status": "qr_ready", "qr_url": qr_login.url, "login_key": login_key}


async def check_qr_login(login_key: str, account_id: int | None = None, domain: str = ""):
    await _cleanup_pending_logins()
    login_data = _pending_logins.get(login_key)
    if not login_data:
        return {"status": "expired"}
    if not _pending_login_allowed(login_data, account_id, domain):
        logger.warning(
            "Rejected QR login check for mismatched login_key=%s account_id=%r domain=%r",
            login_key,
            account_id,
            domain,
        )
        return _login_mismatch_response()

    client = login_data["client"]
    if client.is_connected():
        try:
            me = await client.get_me()
            if me:
                login_data["username"] = me.username
                login_data["phone"] = me.phone
                login_data["session_string"] = client.session.save()
                await client.disconnect()
                _pending_logins.pop(login_key, None)
                return {
                    "status": "authorized",
                    "username": me.username,
                    "phone": me.phone,
                    "first_name": me.first_name,
                    "session_string": login_data["session_string"],
                }
        except Exception as e:
            logger.error(f"check_qr_login error: {e}")

    return {"status": "waiting"}


async def start_phone_login(
    api_id: int,
    api_hash: str,
    phone: str,
    login_key: str,
    account_id: int | None = None,
    domain: str = "",
):
    await _cleanup_pending_logins()
    client = TelegramClient(StringSession(), api_id, api_hash, proxy=PROXY)
    await client.connect()
    await client.send_code_request(phone)

    _pending_logins[login_key] = {
        "client": client,
        "api_id": api_id,
        "api_hash": api_hash,
        "phone": phone,
        "created_at": time.time(),
        "account_id": account_id,
        "domain": str(domain or ""),
    }

    return {"status": "code_sent", "login_key": login_key}


async def complete_phone_login(login_key: str, code: str, account_id: int | None = None, domain: str = ""):
    await _cleanup_pending_logins()
    login_data = _pending_logins.get(login_key)
    if not login_data:
        return {"status": "expired", "error": "Session expired"}
    if not _pending_login_allowed(login_data, account_id, domain):
        logger.warning(
            "Rejected phone login code for mismatched login_key=%s account_id=%r domain=%r",
            login_key,
            account_id,
            domain,
        )
        return _login_mismatch_response()

    client = login_data["client"]
    try:
        await client.sign_in(code=code)
        me = await client.get_me()
        session_string = client.session.save()
        await client.disconnect()
        _pending_logins.pop(login_key, None)

        return {
            "status": "authorized",
            "username": me.username,
            "phone": me.phone,
            "first_name": me.first_name,
            "session_string": session_string,
        }
    except SessionPasswordNeededError:
        login_data["password_required_at"] = time.time()
        return {
            "status": "password_required",
            "error": "Telegram two-factor password required",
            "login_key": login_key,
        }
    except Exception as e:
        if "expired" in str(e).lower() or "invalid" in str(e).lower():
            _pending_logins.pop(login_key, None)
        return {"status": "error", "error": str(e)}


async def complete_phone_password(login_key: str, password: str, account_id: int | None = None, domain: str = ""):
    await _cleanup_pending_logins()
    login_data = _pending_logins.get(login_key)
    if not login_data:
        return {"status": "expired", "error": "Session expired"}
    if not _pending_login_allowed(login_data, account_id, domain):
        logger.warning(
            "Rejected phone login password for mismatched login_key=%s account_id=%r domain=%r",
            login_key,
            account_id,
            domain,
        )
        return _login_mismatch_response()

    client = login_data["client"]
    try:
        await client.sign_in(password=password)
        me = await client.get_me()
        session_string = client.session.save()
        await client.disconnect()
        _pending_logins.pop(login_key, None)

        return {
            "status": "authorized",
            "username": me.username,
            "phone": me.phone,
            "first_name": me.first_name,
            "session_string": session_string,
        }
    except Exception as e:
        text = str(e).lower()
        if "expired" in text or ("invalid" in text and "password" not in text):
            _pending_logins.pop(login_key, None)
        return {"status": "error", "error": str(e)}


async def start_listening(account_id: int, session_string: str, api_id: int, api_hash: str, on_message_callback):
    client = TelegramClient(StringSession(session_string), api_id, api_hash, proxy=PROXY)
    try:
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise PermanentTelegramSessionError("Telegram session is not authorized; re-login required")
        except PermanentTelegramSessionError:
            raise
        except Exception as exc:
            if _is_permanent_session_error(exc):
                raise PermanentTelegramSessionError(str(exc)) from exc
            raise

        _active_clients[account_id] = client
        _listener_restart_attempts[account_id] = 0
        logger.info("Telegram client started for account " + str(account_id))

        def supported_chat_event(event) -> bool:
            return bool(
                getattr(event, "is_private", False) or
                getattr(event, "is_group", False) or
                getattr(event, "is_channel", False)
            )

        async def process_events(events, grouped_id=None, is_edit: bool = False):
            first_event = events[0]
            is_outgoing = bool(getattr(first_event, "out", False))
            chat = await first_event.get_chat()
            sender = await first_event.get_sender()
            private_chat = bool(getattr(first_event, "is_private", False))
            user = sender or chat
            if not user or not chat:
                return

            if private_chat:
                user_name = " ".join(
                    part for part in [getattr(user, "first_name", ""), getattr(user, "last_name", "")]
                    if part
                ).strip() or getattr(user, "title", "") or str(getattr(user, "id", first_event.chat_id))
            else:
                sender_name = " ".join(
                    part for part in [getattr(sender, "first_name", ""), getattr(sender, "last_name", "")]
                    if part
                ).strip() if sender else ""
                sender_name = sender_name or getattr(sender, "title", "") or getattr(sender, "username", "") or "Telegram user"
                chat_title = getattr(chat, "title", "") or str(first_event.chat_id)
                user_name = f"{sender_name} @ {chat_title}" if sender else chat_title

            recipient = ""
            username = getattr(user, "username", None)
            phone = getattr(user, "phone", None)
            if private_chat:
                if username:
                    user_name += " (@" + username + ")"
                    recipient = "@" + username
                elif phone:
                    recipient = "+" + phone
            else:
                recipient = str(first_event.chat_id)

            lines = []
            media_files = []
            for item_event in sorted(events, key=lambda ev: getattr(ev.message, "id", 0) or 0):
                content = await _extract_message_content(client, item_event.message)
                for line in (content.get("text") or "").splitlines():
                    _append_unique(lines, line)
                media_files.extend(content.get("media_files") or [])

            await on_message_callback(
                account_id=account_id,
                chat_id=first_event.chat_id,
                user_id=getattr(user, "id", first_event.chat_id),
                user_name=user_name,
                username=username,
                text="\n".join(lines),
                message_id=f"album_{grouped_id}" if grouped_id else first_event.message.id,
                recipient=recipient,
                media_files=media_files,
                is_outgoing=is_outgoing,
                is_edit=is_edit,
                edit_marker=str(int(getattr(first_event.message, "edit_date", None).timestamp()))
                if is_edit and getattr(first_event.message, "edit_date", None) else "",
            )

        async def flush_grouped_media(key):
            await asyncio.sleep(0.8)
            bucket = _grouped_media_buffers.pop(key, None)
            if not bucket:
                return
            try:
                await process_events(bucket["events"], grouped_id=key[2])
            except Exception:
                logger.exception("Error handling Telegram grouped media")

        async def handler(event):
            if supported_chat_event(event):
                try:
                    is_outgoing = bool(getattr(event, "out", False))
                    if is_outgoing:
                        # Give send_by_entity/send_file a moment to register messages sent by this app.
                        await asyncio.sleep(0.6)
                        if _is_suppressed_outgoing(account_id, event.chat_id, event.message.id):
                            return

                    grouped_id = getattr(event.message, "grouped_id", None)
                    if grouped_id:
                        _cleanup_grouped_media_buffers()
                        key = (account_id, str(event.chat_id), str(grouped_id), "out" if is_outgoing else "in")
                        bucket = _grouped_media_buffers.setdefault(
                            key,
                            {"events": [], "task": None, "created_at": time.time()},
                        )
                        bucket["events"].append(event)
                        if not bucket["task"]:
                            bucket["task"] = asyncio.create_task(flush_grouped_media(key))
                        return

                    await process_events([event])
                except Exception:
                    logger.exception("Error handling Telegram message")

        async def edited_handler(event):
            if not supported_chat_event(event):
                return
            if bool(getattr(event, "out", False)):
                return
            try:
                await process_events([event], is_edit=True)
            except Exception:
                logger.exception("Error handling Telegram edited message")

        client.add_event_handler(handler, telethon.events.NewMessage)
        client.add_event_handler(edited_handler, telethon.events.MessageEdited)
        await client.run_until_disconnected()
    except asyncio.CancelledError:
        logger.info("Telegram listener cancelled for account " + str(account_id))
        raise
    except PermanentTelegramSessionError as exc:
        _stopping_clients.add(account_id)
        await _mark_account_failed(account_id, str(exc))
        logger.exception("Telegram listener requires reauthorization for account " + str(account_id))
        raise
    except Exception:
        logger.exception("Telegram listener failed for account " + str(account_id))
        raise
    finally:
        if _active_clients.get(account_id) is client:
            _active_clients.pop(account_id, None)
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception:
            logger.exception("Failed to disconnect Telegram client for account " + str(account_id))
        logger.info("Telegram listener stopped for account " + str(account_id))


def start_listener_task(account_id: int, session_string: str, api_id: int, api_hash: str, on_message_callback):
    existing = _listener_tasks.get(account_id)
    if existing and not existing.done():
        return existing

    task = asyncio.create_task(
        start_listening(account_id, session_string, api_id, api_hash, on_message_callback),
        name=f"telegram-listener-{account_id}",
    )
    _listener_tasks[account_id] = task
    _stopping_clients.discard(account_id)

    def _schedule_restart(delay: int):
        def _restart():
            if account_id in _stopping_clients or account_id in _active_clients:
                return
            existing_task = _listener_tasks.get(account_id)
            if existing_task and not existing_task.done():
                return
            logger.info("Telegram listener immediate restart for account %s after %ss", account_id, delay)
            start_listener_task(account_id, session_string, api_id, api_hash, on_message_callback)

        loop = asyncio.get_running_loop()
        loop.call_later(delay, _restart)

    def _done(done_task):
        if _listener_tasks.get(account_id) is done_task:
            _listener_tasks.pop(account_id, None)
        try:
            done_task.result()
        except asyncio.CancelledError:
            logger.info("Telegram listener task cancelled for account " + str(account_id))
        except PermanentTelegramSessionError:
            logger.exception("Telegram listener task stopped for account %s; reauthorization required", account_id)
            _stopping_clients.add(account_id)
        except Exception:
            attempts = _listener_restart_attempts.get(account_id, 0) + 1
            _listener_restart_attempts[account_id] = attempts
            delay = min(60, 2 ** min(attempts, 5))
            logger.exception("Telegram listener task crashed for account " + str(account_id))
            _schedule_restart(delay)
        else:
            if account_id not in _stopping_clients:
                attempts = _listener_restart_attempts.get(account_id, 0) + 1
                _listener_restart_attempts[account_id] = attempts
                delay = min(60, 2 ** min(attempts, 5))
                logger.warning("Telegram listener task ended unexpectedly for account %s; restart in %ss", account_id, delay)
                _schedule_restart(delay)

    task.add_done_callback(_done)
    return task


async def send_message(account_id: int, chat_id, text: str) -> bool:
    client = _active_clients.get(account_id)
    if not client:
        return False
    try:
        await client.send_message(chat_id, text)
        return True
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")
        return False


async def send_by_entity(account_id: int, recipient, text: str) -> dict:
    client = _active_clients.get(account_id)
    if not client:
        return {"success": False, "error": "Account not active"}
    try:
        msg = await client.send_message(recipient, text)
        peer_id = _message_peer_id(msg)
        _suppress_outgoing_event(account_id, peer_id, getattr(msg, "id", None))
        message_id = getattr(msg, "id", None)
        return {"success": True, "user_id": peer_id, "message_id": message_id, "message_ids": [message_id] if message_id else []}
    except Exception as e:
        if _is_entity_not_found_error(e):
            resolved = await _resolve_dialog_entity(client, recipient)
            if resolved is not None:
                try:
                    msg = await client.send_message(resolved, text)
                    peer_id = _message_peer_id(msg)
                    _suppress_outgoing_event(account_id, peer_id, getattr(msg, "id", None))
                    message_id = getattr(msg, "id", None)
                    return {"success": True, "user_id": peer_id, "message_id": message_id, "message_ids": [message_id] if message_id else []}
                except Exception as retry_exc:
                    logger.error(f"send_by_entity retry error: {retry_exc}")
                    return {"success": False, "error": str(retry_exc)}
        logger.error(f"send_by_entity error: {e}")
        return {"success": False, "error": str(e)}


async def send_file(account_id: int, recipient, file_bytes, filename: str, caption: str = "") -> dict:
    """Send a file/photo to a Telegram chat via the account's client."""
    client = _active_clients.get(account_id)
    if not client:
        return {"success": False, "error": "Account not active"}
    try:
        as_photo = _is_photo_filename(filename)
        attributes = None if as_photo else [DocumentAttributeFilename(filename)]
        if isinstance(file_bytes, (str, os.PathLike)):
            upload_target = str(file_bytes)
        elif hasattr(file_bytes, "read"):
            upload_target = file_bytes
            try:
                upload_target.seek(0)
            except Exception:
                pass
            try:
                upload_target.name = filename
            except Exception:
                pass
        else:
            buf = BytesIO(file_bytes)
            buf.name = filename
            upload_target = buf
        try:
            sent = await client.send_file(recipient, upload_target, caption=caption or None,
                                          attributes=attributes, force_document=not as_photo)
        except Exception as e:
            if not _is_entity_not_found_error(e):
                raise
            resolved = await _resolve_dialog_entity(client, recipient)
            if resolved is None:
                raise
            if hasattr(upload_target, "seek"):
                try:
                    upload_target.seek(0)
                except Exception:
                    pass
            sent = await client.send_file(resolved, upload_target, caption=caption or None,
                                          attributes=attributes, force_document=not as_photo)
        sent_items = sent if isinstance(sent, list) else [sent]
        peer_id = None
        for item in sent_items:
            if item:
                peer_id = _message_peer_id(item) or peer_id
                _suppress_outgoing_event(account_id, peer_id, getattr(item, "id", None))
        message_ids = [getattr(item, "id", None) for item in sent_items if item and getattr(item, "id", None)]
        return {"success": True, "user_id": peer_id, "message_id": message_ids[0] if message_ids else None, "message_ids": message_ids}
    except Exception as e:
        logger.error(f"send_file error for account {account_id}: {e}")
        return {"success": False, "error": str(e)}


async def edit_message(account_id: int, chat_id, message_id, text: str) -> dict:
    client = _active_clients.get(account_id)
    if not client:
        return {"success": False, "error": "Account not active"}
    try:
        try:
            edited = await client.edit_message(chat_id, int(message_id), text)
        except Exception as e:
            if not _is_entity_not_found_error(e):
                raise
            resolved = await _resolve_dialog_entity(client, chat_id)
            if resolved is None:
                raise
            edited = await client.edit_message(resolved, int(message_id), text)
        return {"success": True, "message_id": getattr(edited, "id", message_id)}
    except Exception as e:
        logger.error(f"edit_message error for account {account_id}: {e}")
        return {"success": False, "error": str(e)}


async def send_typing_action(account_id: int, recipient, seconds: float = 1.5) -> dict:
    client = _active_clients.get(account_id)
    if not client:
        return {"success": False, "error": "Account not active"}
    try:
        async with client.action(recipient, "typing"):
            await asyncio.sleep(max(0.1, min(float(seconds or 1.5), 5.0)))
        return {"success": True}
    except Exception as e:
        logger.error(f"send_typing_action error for account {account_id}: {e}")
        return {"success": False, "error": str(e)}


async def stop_client(account_id: int):
    _stopping_clients.add(account_id)
    task = _listener_tasks.pop(account_id, None)
    if task and not task.done():
        task.cancel()
    client = _active_clients.pop(account_id, None)
    if client:
        await client.disconnect()
    _listener_restart_attempts.pop(account_id, None)


async def stop_all():
    for account_id in list(_active_clients.keys()):
        await stop_client(account_id)
