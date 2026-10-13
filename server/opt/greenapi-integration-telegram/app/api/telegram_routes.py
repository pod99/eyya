import json
import logging
import asyncio
import os
import re
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from sqlalchemy import select, update
from app.database import async_session, Tenant, Connector, Channel, ChatLink, Message, TelegramAccount, utcnow
from app.config import settings
from app.bitrix import client as bx
from app.bitrix.crm import create_or_get_lead
from app.outbox import outbox
from app.media_security import signed_media_url
from app.crypto import decrypt_if_encrypted, encrypt_if_plaintext

logger = logging.getLogger(__name__)
router = APIRouter()

MEDIA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "media")


def _get_bitrix_file_type(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
        return "image"
    return "file"


def _primary_line_id(raw_line_id=None) -> int:
    if settings.primary_bitrix_line_id:
        return settings.primary_bitrix_line_id
    return int(raw_line_id or 0)


def _primary_connector_id(raw_connector_id: str = "") -> str:
    return settings.primary_connector_id or raw_connector_id or settings.connector_id


def _safe_bitrix_user_name(user_name: str, username: str = "") -> str:
    raw_name = re.sub(r"\(@[^)]*\)", "", user_name or "").strip()
    raw_name = raw_name or username or "Telegram user"
    safe_name = re.sub(r"[^A-Za-zА-Яа-яЁё\s'-]", " ", raw_name).strip()
    safe_name = re.sub(r"\s+", " ", safe_name)[:25].strip()
    return safe_name or "Telegram user"


def _bitrix_user_from_telegram(user_id, user_name: str, username: str = "") -> dict:
    user = {"id": str(user_id), "name": _safe_bitrix_user_name(user_name, username)}
    if username:
        user["url"] = f"https://t.me/{username}"
    return user


def _build_bitrix_file_entry(public_url: str, media_name: str, media_size: int) -> dict:
    return {
        "url": public_url,
        "link": public_url,
        "name": media_name,
        "type": _get_bitrix_file_type(media_name),
        "size": media_size,
    }


async def _save_media(media_bytes: bytes, media_name: str) -> str:
    """Save media bytes to disk and return public filename."""
    os.makedirs(MEDIA_DIR, exist_ok=True)
    ext = os.path.splitext(media_name)[1] or ".bin"
    filename = f"{uuid.uuid4()}{ext}"
    path = os.path.join(MEDIA_DIR, filename)
    with open(path, "wb") as f:
        f.write(media_bytes)
    return filename


async def _telegram_inbound_callback(account_id, chat_id, user_id, user_name, username,
                                     text, message_id, recipient="",
                                     media_bytes=None, media_name=None,
                                     media_files=None,
                                     is_outgoing: bool = False,
                                     is_edit: bool = False,
                                     edit_marker: str = ""):
    """Called by Telethon handler when a Telegram user sends a message to the account."""
    flow = "outgoing" if is_outgoing else "inbound"
    event_message_id = f"{message_id}_{edit_marker}" if is_edit and edit_marker else str(message_id)
    idempotency_key = f"telegram_{account_id}_{chat_id}_{event_message_id}_{flow}"

    async with async_session() as session:
        # Idempotency check
        existing = await session.execute(
            select(Message).where(Message.idempotency_key == idempotency_key)
        )
        if existing.scalar_one_or_none():
            return

        acc_result = await session.execute(
            select(TelegramAccount).where(TelegramAccount.id == account_id)
        )
        account = acc_result.scalar_one_or_none()
        if not account:
            logger.warning(f"Telegram callback: account {account_id} not found")
            return

        tenant_result = await session.execute(
            select(Tenant).where(Tenant.id == account.tenant_id)
        )
        tenant = tenant_result.scalar_one_or_none()
        if not tenant:
            logger.warning(f"Telegram callback: tenant not found for account {account_id}")
            return

        connector_id = _primary_connector_id(account.bitrix_connector_id)
        line_id = _primary_line_id(account.bitrix_line_id)

        # Find or create ChatLink. Telegram user ids are only unique inside one
        # connected account, so account_id is part of the dialog identity.
        link_result = await session.execute(
            select(ChatLink).where(
                ChatLink.tenant_id == tenant.id,
                ChatLink.provider == connector_id,
                ChatLink.external_chat_id == str(chat_id),
                ChatLink.account_id == account_id,
            ).order_by(ChatLink.bitrix_chat_id.is_(None), ChatLink.id.desc())
        )
        link = link_result.scalars().first()
        if not link:
            legacy_result = await session.execute(
                select(ChatLink).where(
                    ChatLink.tenant_id == tenant.id,
                    ChatLink.provider == connector_id,
                    ChatLink.external_chat_id == str(chat_id),
                    ChatLink.account_id.is_(None),
                ).order_by(ChatLink.bitrix_chat_id.is_(None), ChatLink.id.desc())
            )
            link = legacy_result.scalars().first()
        link_created = False

        if not link:
            channel = None
            channel_result = await session.execute(
                select(Channel).where(
                    Channel.tenant_id == tenant.id,
                    Channel.provider == connector_id,
                    Channel.bitrix_line_id == line_id,
                )
            )
            channel = channel_result.scalars().first()
            link = ChatLink(
                tenant_id=tenant.id,
                channel_id=channel.id if channel else None,
                provider=connector_id,
                external_chat_id=str(chat_id),
                external_user_id=str(user_id),
                external_user_name=user_name,
                bitrix_line_id=line_id,
                account_id=account_id,
            )
            session.add(link)
            await session.commit()
            await session.refresh(link)
            link_created = True
        elif link.bitrix_line_id != line_id or link.account_id != account_id:
            await session.execute(
                update(ChatLink).where(ChatLink.id == link.id).values(
                    bitrix_line_id=line_id,
                    account_id=account_id,
                    external_user_id=str(user_id),
                    external_user_name=user_name,
                    updated_at=utcnow(),
                )
            )
            await session.commit()
            link.bitrix_line_id = line_id
            link.account_id = account_id
        elif link.external_user_name != user_name or link.external_user_id != str(user_id):
            await session.execute(
                update(ChatLink).where(ChatLink.id == link.id).values(
                    external_user_id=str(user_id),
                    external_user_name=user_name,
                    updated_at=utcnow(),
                )
            )
            await session.commit()

        # Build Bitrix24 message payload
        external_message_id = f"tg_{account_id}_{event_message_id}_{flow}"
        payload_text = text or ""
        if is_edit:
            payload_text = f"[Сообщение отредактировано]\n{payload_text}" if payload_text else "[Сообщение отредактировано]"
        msg_payload = {
            "user": _bitrix_user_from_telegram(user_id, user_name, username),
            "message": {
                "id": external_message_id,
                "date": int(datetime.now(timezone.utc).timestamp()),
                "text": payload_text,
                "disable_crm": "Y",
            },
            "chat": {"id": str(chat_id), "name": user_name},
        }

        # Handle media via imconnector message.files only. Do not also commit to IM disk,
        # otherwise Bitrix24 shows the same photo/file twice in the Open Line.
        public_files = []
        normalized_media_files = []
        if media_files:
            for item in media_files:
                if item and item.get("bytes") and item.get("name"):
                    normalized_media_files.append({"bytes": item["bytes"], "name": item["name"]})
        elif media_bytes and media_name:
            normalized_media_files.append({"bytes": media_bytes, "name": media_name})

        for media_item in normalized_media_files:
            item_bytes = media_item["bytes"]
            item_name = media_item["name"]
            try:
                saved_filename = await _save_media(item_bytes, item_name)
                public_url = signed_media_url(saved_filename)
                logger.info(f"Media saved: {saved_filename} ({len(item_bytes)} bytes) for msg {message_id}")
                file_entry = _build_bitrix_file_entry(public_url, item_name, len(item_bytes))
                public_files.append(file_entry)
                msg_payload["message"].setdefault("files", []).append(file_entry)
            except Exception as e:
                logger.warning(f"Failed to save media locally for msg {message_id}: {e}")

        if normalized_media_files and not payload_text:
            sender_ref = f"@{username}" if username else (user_name or str(user_id))
            image_count = len([item for item in normalized_media_files if _get_bitrix_file_type(item["name"]) == "image"])
            if len(normalized_media_files) > 1:
                msg_payload["message"]["text"] = f"Файлы от {sender_ref}: {len(normalized_media_files)}"
            elif image_count:
                msg_payload["message"]["text"] = f"Фото от {sender_ref}"
            else:
                msg_payload["message"]["text"] = f"Файл от {sender_ref}: {normalized_media_files[0]['name']}"
            # If text (caption) is set, keep it as-is regardless of media

        if is_outgoing:
            original_text = msg_payload["message"].get("text") or ""
            if original_text:
                msg_payload["message"]["text"] = f"[Ответ из Telegram]\n{original_text}"
            else:
                msg_payload["message"]["text"] = "[Файл отправлен напрямую из Telegram]"

        # Send to Bitrix24 Open Lines
        bx_result = None
        try:
            bx_result = await bx.send_messages(
                tenant.bitrix_domain,
                tenant.access_token_encrypted,
                connector_id,
                line_id,
                [msg_payload],
            )
            logger.info(f"Sent to Bitrix24 for chat_id={chat_id} flow={flow}: {bx_result}")
        except Exception as e:
            logger.warning(f"Failed to send to Bitrix24 (account={account_id}, chat={chat_id}): {e}")

        bx_status = bx.send_messages_status(bx_result)
        # Extract and save bitrix_chat_id — this is what makes operator reply possible
        new_bitrix_chat_id = None
        new_bitrix_dialog_id = None
        if bx_status["success"]:
            new_bitrix_chat_id = bx_status.get("chat_id") or None
            new_bitrix_dialog_id = bx_status.get("dialog_id") or None

        if new_bitrix_chat_id and str(new_bitrix_chat_id) != str(link.bitrix_chat_id or ""):
            await session.execute(
                update(ChatLink).where(ChatLink.id == link.id).values(
                    bitrix_chat_id=str(new_bitrix_chat_id),
                    bitrix_dialog_id=str(new_bitrix_dialog_id) if new_bitrix_dialog_id else None,
                    last_message_at=utcnow(),
                    updated_at=utcnow(),
                )
            )
            await session.commit()
            logger.info(
                f"ChatLink {link.id}: updated bitrix_chat_id "
                f"{link.bitrix_chat_id!r} -> {new_bitrix_chat_id}"
            )
            link.bitrix_chat_id = str(new_bitrix_chat_id)

        delivery_status = "failed"
        delivery_error = bx_status.get("error")
        if bx_status["success"]:
            if new_bitrix_chat_id or link.bitrix_chat_id:
                delivery_status = "sent"
                delivery_error = None
            else:
                delivery_status = "partial_failed"
                delivery_error = "Bitrix24 accepted message but did not return Open Line CHAT_ID"

        lead_result = None
        if not is_outgoing and (link_created or not link.crm_entity_id):
            try:
                first_text = payload_text or (
                    f"Файлы: {', '.join(item['name'] for item in normalized_media_files[:3])}"
                    if normalized_media_files else ""
                )
                lead_result = await create_or_get_lead(
                    tenant.bitrix_domain,
                    tenant.access_token_encrypted,
                    connector_id,
                    str(chat_id),
                    display_name=user_name,
                    recipient=recipient,
                    first_text=first_text,
                    bitrix_chat_id=link.bitrix_chat_id,
                )
                if lead_result.get("success") and lead_result.get("id"):
                    await session.execute(
                        update(ChatLink).where(ChatLink.id == link.id).values(
                            crm_entity_type="lead",
                            crm_entity_id=str(lead_result["id"]),
                            updated_at=utcnow(),
                        )
                    )
                    await session.commit()
                    logger.info(f"Lead linked to ChatLink {link.id}: {lead_result}")
            except Exception as e:
                logger.warning(f"Could not create lead for inbound chat_id={chat_id}: {e}")

        # Save message record using ORM
        raw_payload = {
            "source": "telegram_edit" if is_edit else ("telegram_outgoing" if is_outgoing else "telegram"),
            "account_id": account_id,
            "chat_id": str(chat_id),
            "user_id": str(user_id),
            "username": username,
            "message_id": str(message_id),
            "event_message_id": event_message_id,
            "direct_outgoing": is_outgoing,
            "edited": is_edit,
        }
        if lead_result:
            raw_payload["lead"] = lead_result
        raw_payload["bitrix_send"] = bx_status
        if normalized_media_files:
            raw_payload["files"] = [
                {
                    "name": item["name"],
                    "size": len(item["bytes"]) if item.get("bytes") else None,
                    "url": public_files[idx]["url"] if idx < len(public_files) else "",
                    "type": _get_bitrix_file_type(item["name"]),
                }
                for idx, item in enumerate(normalized_media_files)
            ]
        m = Message(
            tenant_id=tenant.id,
            chat_link_id=link.id,
            direction="outbound" if is_outgoing else "inbound",
            provider_message_id=str(message_id),
            text=msg_payload["message"].get("text") or "",
            status=delivery_status,
            error=delivery_error,
            raw_payload_json=json.dumps(raw_payload, ensure_ascii=False),
            idempotency_key=idempotency_key,
            account_id=account_id,
            chat_id_tg=str(chat_id),
            sender_name=(f"Telegram @{account.username}" if is_outgoing and account.username else account.phone if is_outgoing and account.phone else user_name),
            recipient=recipient or str(chat_id),
        )
        session.add(m)
        await session.commit()


@router.post("/api/telegram/start-all")
async def telegram_start_all():
    from app.telegram.service import start_listener_task, _active_clients

    async with async_session() as session:
        result = await session.execute(
            select(TelegramAccount).where(TelegramAccount.state.in_(["active", "authorized"]))
        )
        accounts = result.scalars().all()
        started = 0
        for acc in accounts:
            if acc.id not in _active_clients:
                start_listener_task(
                    account_id=acc.id,
                    session_string=decrypt_if_encrypted(acc.session_string),
                    api_id=acc.api_id,
                    api_hash=decrypt_if_encrypted(acc.api_hash),
                    on_message_callback=_telegram_inbound_callback,
                )
                started += 1

    return {"success": True, "started": started, "active": list(_active_clients.keys())}


@router.get("/api/telegram/accounts/{domain}")
async def list_telegram_accounts(domain: str):
    async with async_session() as session:
        result = await session.execute(select(Tenant).where(Tenant.bitrix_domain == domain))
        tenant = result.scalar_one_or_none()
        if not tenant:
            return {"success": False, "message": "Tenant not found"}

        acc_result = await session.execute(
            select(TelegramAccount).where(TelegramAccount.tenant_id == tenant.id)
        )
        accounts = acc_result.scalars().all()
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
                }
                for a in accounts
            ],
        }


@router.post("/api/telegram/login/qr")
async def telegram_login_qr(request: Request):
    body = await request.json()
    domain = body.get("domain")
    api_id = int(body.get("api_id", 0))
    api_hash = body.get("api_hash", "")

    if not api_id or not api_hash:
        return {"success": False, "message": "api_id and api_hash required"}

    async with async_session() as session:
        result = await session.execute(select(Tenant).where(Tenant.bitrix_domain == domain))
        tenant = result.scalar_one_or_none()
        if not tenant:
            return {"success": False, "message": "Tenant not found"}

    from app.telegram.service import start_qr_login
    account = None
    async with async_session() as session:
        account = TelegramAccount(
            tenant_id=tenant.id,
            api_id=api_id,
            api_hash=encrypt_if_plaintext(api_hash),
            state="qr_pending",
        )
        session.add(account)
        await session.commit()
        await session.refresh(account)

    login_key = f"{domain}_{account.id}_{uuid.uuid4().hex}"
    try:
        qr_result = await start_qr_login(api_id, api_hash, login_key, account_id=account.id, domain=domain)

        return {
            "success": True,
            "qr_url": qr_result.get("qr_url"),
            "login_key": login_key,
            "account_id": account.id,
        }
    except Exception as e:
        async with async_session() as session:
            await session.execute(
                update(TelegramAccount).where(TelegramAccount.id == account.id).values(state="failed")
            )
            await session.commit()
        return {"success": False, "error": str(e)}


@router.post("/api/telegram/login/phone")
async def telegram_login_phone(request: Request):
    body = await request.json()
    domain = body.get("domain")
    try:
        account_id = int(body.get("account_id") or 0)
    except (TypeError, ValueError):
        account_id = 0
    try:
        api_id = int(body.get("api_id") or 0)
    except (TypeError, ValueError):
        api_id = 0
    api_hash = body.get("api_hash", "")
    phone = body.get("phone", "")

    async with async_session() as session:
        result = await session.execute(select(Tenant).where(Tenant.bitrix_domain == domain))
        tenant = result.scalar_one_or_none()
        if not tenant:
            return {"success": False, "message": "Tenant not found"}

    from app.telegram.service import start_phone_login
    async with async_session() as session:
        if account_id:
            result = await session.execute(
                select(TelegramAccount).where(
                    TelegramAccount.id == account_id,
                    TelegramAccount.tenant_id == tenant.id,
                )
            )
            account = result.scalar_one_or_none()
            if not account:
                return {"success": False, "message": "Telegram account not found"}
            api_id = api_id or account.api_id
            api_hash = api_hash or decrypt_if_encrypted(account.api_hash)
            phone = phone or account.phone or ""
            if not api_id or not api_hash or not phone:
                return {"success": False, "message": "api_id, api_hash and phone required"}
            await session.execute(
                update(TelegramAccount)
                .where(TelegramAccount.id == account.id)
                .values(
                    api_id=api_id,
                    api_hash=encrypt_if_plaintext(api_hash),
                    phone=phone,
                    session_string=None,
                    state="code_pending",
                    updated_at=utcnow(),
                )
            )
            await session.commit()
        else:
            if not api_id or not api_hash or not phone:
                return {"success": False, "message": "api_id, api_hash and phone required"}
            account = TelegramAccount(
                tenant_id=tenant.id,
                api_id=api_id,
                api_hash=encrypt_if_plaintext(api_hash),
                phone=phone,
                state="code_pending",
            )
            session.add(account)
            await session.commit()
            await session.refresh(account)

    login_key = f"{domain}_{account.id}_{uuid.uuid4().hex}"
    try:
        await start_phone_login(api_id, api_hash, phone, login_key, account_id=account.id, domain=domain)

        return {"success": True, "login_key": login_key, "account_id": account.id}
    except Exception as e:
        async with async_session() as session:
            await session.execute(
                update(TelegramAccount).where(TelegramAccount.id == account.id).values(state="failed", updated_at=utcnow())
            )
            await session.commit()
        return {"success": False, "error": str(e)}


@router.post("/api/telegram/login/code")
async def telegram_login_code(request: Request):
    body = await request.json()
    login_key = body.get("login_key", "")
    code = body.get("code", "")
    try:
        account_id = int(body.get("account_id") or 0)
    except (TypeError, ValueError):
        account_id = 0

    if not login_key or not code or not account_id:
        return {"success": False, "message": "login_key, account_id and code required"}

    async with async_session() as session:
        acc_result = await session.execute(
            select(TelegramAccount).where(
                TelegramAccount.id == account_id,
                TelegramAccount.state == "code_pending",
            )
        )
        account = acc_result.scalar_one_or_none()
        if not account:
            return {"success": False, "error": "Login account not found or not waiting for code"}
        tenant = await session.get(Tenant, account.tenant_id)
        account_domain = tenant.bitrix_domain if tenant else ""

    from app.telegram.service import complete_phone_login
    result = await complete_phone_login(login_key, code, account_id=account_id, domain=account_domain)

    if result["status"] == "authorized":
        async with async_session() as session:
            acc_result = await session.execute(
                select(TelegramAccount).where(
                    TelegramAccount.id == account_id,
                    TelegramAccount.state == "code_pending",
                )
            )
            account = acc_result.scalar_one_or_none()
            if account:
                await session.execute(
                    update(TelegramAccount).where(TelegramAccount.id == account.id).values(
                        session_string=encrypt_if_plaintext(result["session_string"]),
                        username=result.get("username"),
                        phone=result.get("phone"),
                        state="authorized",
                    )
                )
                await session.commit()

        return {"success": True, "username": result.get("username"), "phone": result.get("phone")}

    if result["status"] == "password_required":
        return {
            "success": False,
            "password_required": True,
            "login_key": login_key,
            "account_id": account_id,
            "error": result.get("error", "Telegram two-factor password required"),
        }

    return {"success": False, "error": result.get("error", "Login failed")}


@router.post("/api/telegram/login/password")
async def telegram_login_password(request: Request):
    body = await request.json()
    login_key = body.get("login_key", "")
    password = body.get("password", "")
    try:
        account_id = int(body.get("account_id") or 0)
    except (TypeError, ValueError):
        account_id = 0

    if not login_key or not password or not account_id:
        return {"success": False, "message": "login_key, account_id and password required"}

    async with async_session() as session:
        acc_result = await session.execute(
            select(TelegramAccount).where(
                TelegramAccount.id == account_id,
                TelegramAccount.state == "code_pending",
            )
        )
        account = acc_result.scalar_one_or_none()
        if not account:
            return {"success": False, "error": "Login account not found or not waiting for password"}
        tenant = await session.get(Tenant, account.tenant_id)
        account_domain = tenant.bitrix_domain if tenant else ""

    from app.telegram.service import complete_phone_password
    result = await complete_phone_password(login_key, password, account_id=account_id, domain=account_domain)

    if result["status"] == "authorized":
        async with async_session() as session:
            acc_result = await session.execute(
                select(TelegramAccount).where(
                    TelegramAccount.id == account_id,
                    TelegramAccount.state == "code_pending",
                )
            )
            account = acc_result.scalar_one_or_none()
            if account:
                await session.execute(
                    update(TelegramAccount).where(TelegramAccount.id == account.id).values(
                        session_string=encrypt_if_plaintext(result["session_string"]),
                        username=result.get("username"),
                        phone=result.get("phone"),
                        state="authorized",
                    )
                )
                await session.commit()

        return {"success": True, "username": result.get("username"), "phone": result.get("phone")}

    return {"success": False, "error": result.get("error", "Login failed")}


@router.post("/api/telegram/login/check")
async def telegram_login_check(request: Request):
    body = await request.json()
    login_key = body.get("login_key", "")
    try:
        account_id = int(body.get("account_id") or 0)
    except (TypeError, ValueError):
        account_id = 0

    if not login_key or not account_id:
        return {"status": "expired", "error": "login_key and account_id required"}

    async with async_session() as session:
        acc_result = await session.execute(
            select(TelegramAccount).where(
                TelegramAccount.id == account_id,
                TelegramAccount.state == "qr_pending",
            )
        )
        account_for_check = acc_result.scalar_one_or_none()
        if not account_for_check:
            return {"status": "expired", "error": "Login account not found or not waiting for QR"}
        tenant_for_check = await session.get(Tenant, account_for_check.tenant_id)
        account_domain = tenant_for_check.bitrix_domain if tenant_for_check else ""

    from app.telegram.service import check_qr_login
    result = await check_qr_login(login_key, account_id=account_id, domain=account_domain)

    if result["status"] == "authorized":
        async with async_session() as session:
            acc_result = await session.execute(
                select(TelegramAccount).where(
                    TelegramAccount.id == account_id,
                    TelegramAccount.state == "qr_pending",
                )
            )
            account = acc_result.scalar_one_or_none()
            if account:
                await session.execute(
                    update(TelegramAccount).where(TelegramAccount.id == account.id).values(
                        session_string=encrypt_if_plaintext(result["session_string"]),
                        username=result.get("username"),
                        phone=result.get("phone"),
                        state="authorized",
                    )
                )
                await session.commit()

                from app.telegram.service import start_listener_task, _active_clients
                if account.id not in _active_clients:
                    start_listener_task(
                        account_id=account.id,
                        session_string=result["session_string"],
                        api_id=account.api_id,
                        api_hash=decrypt_if_encrypted(account.api_hash),
                        on_message_callback=_telegram_inbound_callback,
                    )

    return result


@router.get("/api/telegram/qr-image/{login_key:path}")
async def telegram_qr_image(login_key: str):
    from app.telegram.service import _pending_logins
    from io import BytesIO
    try:
        import qrcode
    except ImportError:
        return HTMLResponse(content="<p>qrcode module not installed</p>")

    login_data = _pending_logins.get(login_key)
    if not login_data or not login_data.get("qr_url"):
        return HTMLResponse(content="<p>QR not ready</p>")

    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(login_data["qr_url"])
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


@router.post("/api/telegram/connect")
async def telegram_connect(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()
    domain = body.get("domain")
    account_id = int(body.get("account_id", 0))
    line_id = _primary_line_id(body.get("line_id", 0))

    async with async_session() as session:
        acc_result = await session.execute(
            select(TelegramAccount).where(TelegramAccount.id == account_id)
        )
        account = acc_result.scalar_one_or_none()
        if not account or not account.session_string:
            return {"success": False, "message": "Account not found or not authorized"}

        connector_id = _primary_connector_id(f"tg_{account.id}")
        name = f"Telegram @{account.username}" if account.username else f"Telegram {account.phone}"

        tenant_result = await session.execute(
            select(Tenant).where(Tenant.id == account.tenant_id)
        )
        tenant = tenant_result.scalar_one_or_none()
        enc_token = tenant.access_token_encrypted if tenant else None

        if enc_token:
            try:
                await bx.register_connector(
                    domain, enc_token, connector_id, name, f"{settings.app_url}/bitrix/install"
                )
                await bx.activate_connector(domain, enc_token, connector_id, line_id, 1)
                await bx.set_connector_data(domain, enc_token, connector_id, line_id, {
                    "ID": f"{connector_id}_line_{line_id}",
                    "NAME": name,
                    "URL": f"{settings.app_url}/bitrix/install",
                    "URL_IM": f"{settings.app_url}/bitrix/widget",
                })
                await bx.bind_event(
                    domain, enc_token, "ONIMCONNECTORMESSAGEADD", f"{settings.app_url}/bitrix/events"
                )
                await bx.bind_event(
                    domain, enc_token, "ONIMCONNECTORMESSAGEUPDATE", f"{settings.app_url}/bitrix/events"
                )
            except Exception as e:
                logger.warning(f"Bitrix24 API calls failed during connect: {e}")

        await session.execute(
            update(TelegramAccount).where(TelegramAccount.id == account_id).values(
                bitrix_line_id=line_id,
                bitrix_connector_id=connector_id,
                state="active",
            )
        )
        await session.commit()

        # Create/update channel record
        existing_ch = await session.execute(
            select(Channel).where(
                Channel.tenant_id == account.tenant_id,
                Channel.provider == connector_id,
            )
        )
        if not existing_ch.scalar_one_or_none():
            ch = Channel(
                tenant_id=account.tenant_id,
                provider=connector_id,
                name=name,
                bitrix_line_id=line_id,
                status="active",
            )
            session.add(ch)
            await session.commit()

    background_tasks.add_task(
        _start_listener_bg,
        account_id=account_id,
        session_string=decrypt_if_encrypted(account.session_string),
        api_id=account.api_id,
        api_hash=decrypt_if_encrypted(account.api_hash),
    )

    return {"success": True, "message": f"{name} connected to line {line_id}",
            "connector_id": connector_id}


async def _start_listener_bg(account_id, session_string, api_id, api_hash):
    from app.telegram.service import start_listener_task
    start_listener_task(
        account_id=account_id,
        session_string=session_string,
        api_id=api_id,
        api_hash=api_hash,
        on_message_callback=_telegram_inbound_callback,
    )
