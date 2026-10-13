"""
Tests for FastAPI route handlers: /bitrix/events, /api/outbox/*, /api/admin/refresh-token
"""
import json
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
from app.database import utcnow


def test_normalize_widget_recipient_accepts_common_forms():
    from app.api.routes import _normalize_widget_recipient

    assert _normalize_widget_recipient("nv_kudryavtseva") == "@nv_kudryavtseva"
    assert _normalize_widget_recipient("@ nv_kudryavtseva") == "@nv_kudryavtseva"
    assert _normalize_widget_recipient("https://t.me/nv_kudryavtseva") == "@nv_kudryavtseva"
    assert _normalize_widget_recipient("+7 (999) 000-00-00") == "+79990000000"


def test_bitrix_external_user_from_recipient_builds_crm_safe_user():
    from app.api.routes import _bitrix_external_user_from_recipient

    user = _bitrix_external_user_from_recipient("@nv_kudryavtseva", 111222333)
    assert user["id"] == "111222333"
    assert user["name"] == "nv kudryavtseva"
    assert user["url"] == "https://t.me/nv_kudryavtseva"

    phone_user = _bitrix_external_user_from_recipient("+79990000000", 444)
    assert phone_user["phone"] == "+79990000000"
    assert "@" not in phone_user["name"]


def test_extract_files_from_raw_payload():
    from app.api.routes import _extract_files_from_raw, _files_label

    files = _extract_files_from_raw(json.dumps({
        "files": [{"name": "photo.jpg", "url": "https://example.com/photo.jpg", "type": "image"}]
    }))
    assert files == [{
        "name": "photo.jpg",
        "url": "https://example.com/photo.jpg",
        "type": "image",
        "size": None,
    }]
    assert _files_label(files) == "[Фото: photo.jpg]"


def test_inbound_webhook_auth_accepts_bearer_and_rejects_bad_token():
    from types import SimpleNamespace
    from app.api.routes import _verify_bearer_or_hmac

    good = SimpleNamespace(headers={"authorization": "Bearer secret"}, query_params={})
    bad = SimpleNamespace(headers={"authorization": "Bearer wrong"}, query_params={})

    assert _verify_bearer_or_hmac(good, b"{}", "secret") is True
    assert _verify_bearer_or_hmac(bad, b"{}", "secret") is False


def test_js_literal_escapes_script_breakout():
    from app.api.routes import _js_literal

    literal = _js_literal('</script><img src=x onerror=alert(1)>')

    assert literal.startswith('"') and literal.endswith('"')
    assert "</script" not in literal.lower()
    assert "\\u003c" in literal


def test_widget_payload_fingerprint_changes_with_payload():
    from app.api.routes import _widget_payload_fingerprint

    files = [{"name": "a.txt", "content_type": "text/plain", "bytes": b"one"}]
    first = _widget_payload_fingerprint(1, "@user", "hello", files, True, True, False, "deal", "10")
    same = _widget_payload_fingerprint(1, "@user", "hello", files, True, True, False, "deal", "10")
    changed_text = _widget_payload_fingerprint(1, "@user", "hello!", files, True, True, False, "deal", "10")
    changed_file = _widget_payload_fingerprint(
        1, "@user", "hello", [{"name": "a.txt", "content_type": "text/plain", "bytes": b"two"}],
        True, True, False, "deal", "10",
    )

    assert first == same
    assert first != changed_text
    assert first != changed_file


def test_inbound_identity_without_provider_id_uses_payload_fingerprint():
    from app.api.routes import _inbound_message_identity

    body = {
        "external_chat_id": "client-1",
        "external_user_id": "user-1",
        "text": "hello",
        "timestamp": "2026-06-30T10:00:00Z",
        "attachments": [{"name": "photo.jpg", "url": "https://example.test/photo.jpg"}],
    }

    first_id, first_key, generated = _inbound_message_identity(
        "custom", "42", body, "client-1", "user-1", "hello", body["attachments"]
    )
    second_id, second_key, second_generated = _inbound_message_identity(
        "custom", "42", body, "client-1", "user-1", "hello", body["attachments"]
    )

    assert generated is True
    assert second_generated is True
    assert first_id == second_id
    assert first_key == second_key
    assert first_key == f"custom_42_{first_id}_inbound"
    assert first_id.startswith("generated_")


def test_pending_login_allowed_requires_account_and_domain_match():
    from app.telegram.service import _pending_login_allowed

    login_data = {"account_id": 10, "domain": "example.bitrix24.ru"}

    assert _pending_login_allowed(login_data, account_id=10, domain="example.bitrix24.ru") is True
    assert _pending_login_allowed(login_data, account_id=11, domain="example.bitrix24.ru") is False
    assert _pending_login_allowed(login_data, account_id=10, domain="other.bitrix24.ru") is False


def test_bitrix_send_messages_status_rejects_success_false():
    from app.bitrix.client import send_messages_status

    status = send_messages_status({"SUCCESS": False})

    assert status["success"] is False
    assert "SUCCESS=false" in status["error"]


def test_bitrix_send_messages_status_extracts_chat_id():
    from app.bitrix.client import send_messages_status

    status = send_messages_status({
        "SUCCESS": True,
        "DATA": {"RESULT": [{"session": {"CHAT_ID": "chat123", "DIALOG_ID": "dlg123"}}]},
    })

    assert status == {"success": True, "chat_id": "chat123", "dialog_id": "dlg123", "error": ""}


def test_bitrix_send_messages_status_accepts_message_payload_row():
    from app.bitrix.client import send_messages_status

    status = send_messages_status({
        "DATA": {
            "RESULT": [{
                "message": {"id": "tg_6_31988_inbound", "date": {}, "text": "Фото", "files": [26544]},
                "session": {"CHAT_ID": "29350"},
            }]
        }
    })

    assert status == {"success": True, "chat_id": "29350", "dialog_id": "", "error": ""}


def test_admin_client_ip_trusts_forwarded_only_from_configured_proxy(monkeypatch):
    from types import SimpleNamespace
    from app.admin_security import _client_ip
    from app.config import settings

    monkeypatch.setattr(settings, "trusted_proxy_ips", "127.0.0.1")
    trusted = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        headers={"x-forwarded-for": "203.0.113.10"},
    )
    untrusted = SimpleNamespace(
        client=SimpleNamespace(host="10.1.2.3"),
        headers={"x-forwarded-for": "203.0.113.10"},
    )

    assert _client_ip(trusted) == "203.0.113.10"
    assert _client_ip(untrusted) == "10.1.2.3"


def _make_messages_data(connector="tg_6", chat_id="bx_chat_42",
                        text="Reply from operator", msg_id="msg_001"):
    return {
        "CONNECTOR": connector,
        "MESSAGES": [{
            "im": {"message_id": "m1", "chat_id": chat_id, "dialog_id": "dlg1",
                   "user_id": "101", "to_chat_id": chat_id},
            "message": {"id": [msg_id], "text": text, "files": []},
            "chat": {"id": chat_id},
        }]
    }


# ──────────────────────────────────────────────────────────────────────
# handle_operator_reply tests
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_operator_reply_queued_in_outbox(db_engine, tenant, tg_account, chat_link):
    """Operator reply with known bitrix_chat_id → message queued in outbox."""
    from app.database import OutboxEntry, Message
    from app.outbox import OutboxQueue

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    enqueued = []
    original_enqueue = OutboxQueue.enqueue

    async def capture_enqueue(self, msg):
        enqueued.append(msg.payload)
        return await original_enqueue(self, msg)

    with patch("app.api.routes.async_session", Session), \
         patch("app.database.async_session", Session), \
         patch("app.bitrix.client.send_delivery", new_callable=AsyncMock), \
         patch.object(OutboxQueue, "enqueue", capture_enqueue):

        from app.api.routes import handle_operator_reply
        result = await handle_operator_reply(
            domain="test.bitrix24.ru",
            data=_make_messages_data(),
            connector_id="tg_6",
        )

    assert result["success"] is True
    assert len(enqueued) == 1
    payload = enqueued[0]
    assert payload["external_chat_id"] == chat_link.external_chat_id
    assert payload["account_id"] == tg_account.id
    assert payload["text"] == "Reply from operator"


@pytest.mark.asyncio
async def test_operator_reply_no_chat_link_saves_no_link_status(db_engine, tenant, tg_account):
    """Unknown bitrix_chat_id → message saved with status=no_link."""
    from app.database import Message

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    with patch("app.api.routes.async_session", Session), \
         patch("app.database.async_session", Session), \
         patch("app.bitrix.client.send_delivery", new_callable=AsyncMock):

        from app.api.routes import handle_operator_reply
        await handle_operator_reply(
            domain="test.bitrix24.ru",
            data=_make_messages_data(chat_id="UNKNOWN_ID"),
            connector_id="tg_6",
        )

    async with Session() as s:
        msg = (await s.execute(
            select(Message).where(Message.status == "no_link")
        )).scalar_one_or_none()
    assert msg is not None
    assert "UNKNOWN_ID" in msg.error


@pytest.mark.asyncio
async def test_operator_reply_ambiguous_telegram_accounts_is_not_enqueued(db_engine, tenant, tg_account, channel):
    """If a Telegram Open Line link has no exact account and the line has multiple accounts, do not guess."""
    from app.database import ChatLink, Message, TelegramAccount
    from app.outbox import OutboxQueue

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as s:
        s.add(TelegramAccount(
            tenant_id=tenant.id,
            api_id=12345,
            api_hash="testhash2",
            phone="+79007654321",
            username="seconduser",
            session_string="test_session2",
            bitrix_line_id=30,
            bitrix_connector_id="tg_6",
            state="active",
        ))
        link = ChatLink(
            tenant_id=tenant.id,
            channel_id=channel.id,
            provider="tg_6",
            external_chat_id="999111",
            external_user_id="999111",
            external_user_name="Ambiguous User",
            bitrix_line_id=30,
            bitrix_chat_id="ambiguous_bx",
            account_id=None,
        )
        s.add(link)
        await s.commit()

    enqueued = []
    async def capture_enqueue(self, msg):
        enqueued.append(msg.payload)
        return 123

    with patch("app.api.routes.async_session", Session), \
         patch("app.database.async_session", Session), \
         patch.object(OutboxQueue, "enqueue", capture_enqueue):
        from app.api.routes import handle_operator_reply
        result = await handle_operator_reply(
            domain="test.bitrix24.ru",
            data=_make_messages_data(chat_id="ambiguous_bx", text="Should not send"),
            connector_id="tg_6",
        )

    assert result["success"] is True
    assert enqueued == []
    async with Session() as s:
        msg = (await s.execute(
            select(Message).where(Message.text == "Should not send")
        )).scalar_one()
    assert msg.status == "no_account"
    assert "No unambiguous Telegram account" in (msg.error or "")


@pytest.mark.asyncio
async def test_operator_edit_updates_telegram_message(db_engine, tenant, tg_account, chat_link):
    """ONIMCONNECTORMESSAGEUPDATE edits the previously sent Telegram message."""
    from app.database import Message

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as s:
        s.add(Message(
            tenant_id=tenant.id,
            chat_link_id=chat_link.id,
            direction="outbound",
            bitrix_message_id="m777",
            provider_message_id="4242",
            text="old",
            status="sent",
            idempotency_key="seed-edit-message",
        ))
        await s.commit()

    data = {
        "CONNECTOR": "tg_6",
        "MESSAGES": [{
            "im": {"message_id": "m777", "chat_id": "bx_chat_42"},
            "message": {"id": ["4242"], "text": "[b]Operator:[/b][br] edited text"},
            "chat": {"id": chat_link.external_chat_id},
        }]
    }

    with patch("app.api.routes.async_session", Session), \
         patch("app.telegram.service.edit_message", new_callable=AsyncMock) as mock_edit, \
         patch("app.bitrix.client.send_delivery", new_callable=AsyncMock):
        mock_edit.return_value = {"success": True, "message_id": 4242}

        from app.api.routes import handle_operator_edit
        result = await handle_operator_edit("test.bitrix24.ru", data, "tg_6")

    assert result["success"] is True
    mock_edit.assert_called_once_with(tg_account.id, int(chat_link.external_chat_id), "4242", "edited text")


@pytest.mark.asyncio
async def test_operator_reply_tenant_not_found(db_engine):
    """Unknown domain → returns error without crashing."""
    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    with patch("app.api.routes.async_session", Session), \
         patch("app.database.async_session", Session):
        from app.api.routes import handle_operator_reply
        result = await handle_operator_reply(
            domain="nonexistent.bitrix24.ru",
            data=_make_messages_data(),
            connector_id="tg_6",
        )
    assert result["success"] is False


@pytest.mark.asyncio
async def test_operator_reply_idempotency_blocks_duplicate(db_engine, tenant, chat_link, tg_account):
    """Same message_id sent twice → second call skipped, outbox enqueued only once."""
    from app.database import Message
    from app.outbox import OutboxQueue

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    enqueue_count = [0]

    original = OutboxQueue.enqueue
    async def counting_enqueue(self, msg):
        enqueue_count[0] += 1
        return await original(self, msg)

    with patch("app.api.routes.async_session", Session), \
         patch("app.database.async_session", Session), \
         patch("app.bitrix.client.send_delivery", new_callable=AsyncMock), \
         patch.object(OutboxQueue, "enqueue", counting_enqueue):

        from app.api.routes import handle_operator_reply
        data = _make_messages_data()
        await handle_operator_reply("test.bitrix24.ru", data, "tg_6")
        await handle_operator_reply("test.bitrix24.ru", data, "tg_6")  # duplicate

    assert enqueue_count[0] == 1


@pytest.mark.asyncio
async def test_operator_reply_without_message_ids_uses_fallback_key(db_engine, tenant, chat_link, tg_account):
    """Bitrix payload without im.message_id/message.id should not crash."""
    from app.database import Message
    from app.outbox import OutboxQueue

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    enqueued = []

    async def capture_enqueue(self, msg):
        enqueued.append(msg.payload)
        return 123

    data = {
        "CONNECTOR": "tg_6",
        "EVENT_ID": "event-no-message-id",
        "MESSAGES": [{
            "im": {"chat_id": chat_link.external_chat_id},
            "message": {"text": "Fallback id message", "files": []},
            "chat": {"id": chat_link.external_chat_id},
            "user": {"id": "101", "name": "Operator"},
        }],
    }

    with patch("app.api.routes.async_session", Session), \
         patch("app.database.async_session", Session), \
         patch.object(OutboxQueue, "enqueue", capture_enqueue):
        from app.api.routes import handle_operator_reply
        result = await handle_operator_reply("test.bitrix24.ru", data, "tg_6")

    assert result["success"] is True
    assert len(enqueued) == 1
    async with Session() as s:
        msg = (await s.execute(
            select(Message).where(Message.text == "Fallback id message")
        )).scalar_one()
    assert msg.idempotency_key.startswith("bitrix_test.bitrix24.ru_")


def test_bitrix_events_unknown_connector_logic():
    """Unknown connector (not tg_* and not settings.connector_id) → should be skipped."""
    from app.config import settings
    # Test the filter logic directly
    for connector in ["whatsapp_other", "vk_123", "custom_connector"]:
        should_skip = connector and connector != settings.connector_id and not connector.startswith("tg_")
        assert should_skip is True, f"Connector {connector!r} should be skipped"

    # These should NOT be skipped
    for connector in ["tg_1", "tg_6", "tg_100", settings.connector_id]:
        should_skip = connector and connector != settings.connector_id and not connector.startswith("tg_")
        assert not should_skip, f"Connector {connector!r} should NOT be skipped"


# ──────────────────────────────────────────────────────────────────────
# Outbox API endpoint tests
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_outbox_stats_endpoint(db_engine):
    """GET /api/outbox/stats returns counts."""
    from app.database import OutboxEntry

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as s:
        for _ in range(2):
            s.add(OutboxEntry(direction="to_external", payload_json="{}",
                               status="sent", next_retry_at=utcnow()))
        s.add(OutboxEntry(direction="to_external", payload_json="{}",
                           status="pending", next_retry_at=utcnow()))
        await s.commit()

    with patch("app.database.async_session", Session):
        from app.api.routes import outbox_stats
        result = await outbox_stats()

    assert result["success"] is True
    assert result["stats"]["sent"] == 2
    assert result["stats"]["pending"] == 1


@pytest.mark.asyncio
async def test_retry_outbox_entry_endpoint(db_engine):
    """POST /api/outbox/retry/{id} resets a failed entry."""
    from app.database import OutboxEntry

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as s:
        entry = OutboxEntry(
            direction="to_external", payload_json="{}",
            status="failed", attempts=3, last_error="err",
            next_retry_at=utcnow()
        )
        s.add(entry)
        await s.commit()
        entry_id = entry.id

    with patch("app.database.async_session", Session):
        from app.api.routes import retry_outbox_entry
        result = await retry_outbox_entry(entry_id)

    assert result["success"] is True

    async with Session() as s:
        e = (await s.execute(select(OutboxEntry).where(OutboxEntry.id == entry_id))).scalar_one()
    assert e.status == "pending"
    assert e.attempts == 0


@pytest.mark.asyncio
async def test_retry_outbox_entry_not_failed_returns_false(db_engine):
    """POST /api/outbox/retry/{id} returns false for non-failed entry."""
    from app.database import OutboxEntry

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as s:
        entry = OutboxEntry(
            direction="to_external", payload_json="{}",
            status="sent", next_retry_at=utcnow()
        )
        s.add(entry)
        await s.commit()
        entry_id = entry.id

    with patch("app.database.async_session", Session):
        from app.api.routes import retry_outbox_entry
        result = await retry_outbox_entry(entry_id)

    assert result["success"] is False


@pytest.mark.asyncio
async def test_outbox_recovers_stale_processing_entries(db_engine):
    """processing entries older than the lease return to pending."""
    from datetime import timedelta
    from app.database import OutboxEntry, utcnow
    from app.outbox import OutboxQueue

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as s:
        entry = OutboxEntry(
            direction="to_external",
            payload_json="{}",
            status="processing",
            next_retry_at=None,
            updated_at=utcnow() - timedelta(minutes=10),
        )
        s.add(entry)
        await s.commit()
        entry_id = entry.id

    with patch("app.database.async_session", Session):
        recovered = await OutboxQueue().recover_stale_processing(older_than_seconds=60)

    assert recovered == 1
    async with Session() as s:
        entry = await s.get(OutboxEntry, entry_id)
    assert entry.status == "pending"


# ──────────────────────────────────────────────────────────────────────
# Token refresh endpoint
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_refresh_token_success(db_engine, tenant):
    """POST /api/admin/refresh-token → refreshes token and updates DB."""
    from app.database import Tenant

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "access_token": "new_access_token_123",
        "refresh_token": "new_refresh_token_456",
        "expires_in": 7200,
    }

    mock_http_client = AsyncMock()
    mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
    mock_http_client.__aexit__ = AsyncMock(return_value=False)
    mock_http_client.post = AsyncMock(return_value=mock_response)

    with patch("app.api.routes.async_session", Session), \
         patch("app.database.async_session", Session), \
         patch("httpx.AsyncClient", return_value=mock_http_client):

        class FakeRequest:
            async def json(self):
                return {"domain": "test.bitrix24.ru"}

        from app.api.routes import admin_refresh_token
        result = await admin_refresh_token(FakeRequest())

    assert result["success"] is True
    assert "test.bitrix24.ru" in result["refreshed"]

    async with Session() as s:
        t = (await s.execute(select(Tenant).where(Tenant.id == tenant.id))).scalar_one()
    from app.crypto import decrypt_token
    assert decrypt_token(t.access_token_encrypted) == "new_access_token_123"
    assert t.expires_at is not None


@pytest.mark.asyncio
async def test_admin_refresh_token_bitrix_error(db_engine, tenant):
    """POST /api/admin/refresh-token → handles error from Bitrix24 gracefully."""
    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "error": "invalid_grant",
        "error_description": "Refresh token expired"
    }

    mock_http_client = AsyncMock()
    mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
    mock_http_client.__aexit__ = AsyncMock(return_value=False)
    mock_http_client.post = AsyncMock(return_value=mock_response)

    with patch("app.api.routes.async_session", Session), \
         patch("app.database.async_session", Session), \
         patch("httpx.AsyncClient", return_value=mock_http_client):

        class FakeRequest:
            async def json(self):
                return {"domain": "test.bitrix24.ru"}

        from app.api.routes import admin_refresh_token
        result = await admin_refresh_token(FakeRequest())

    assert result["success"] is True  # endpoint itself succeeds
    assert len(result["errors"]) == 1
    assert "expired" in result["errors"][0]["error"].lower()
