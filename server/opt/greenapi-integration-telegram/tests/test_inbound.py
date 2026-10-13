"""
Tests for _telegram_inbound_callback — all edge cases.
"""
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from sqlalchemy import select


SUCCESS_RESULT = {
    "SUCCESS": True,
    "DATA": {
        "RESULT": [{"session": {"CHAT_ID": "bx99", "DIALOG_ID": "dlg99"}}]
    }
}
FAIL_RESULT = {"SUCCESS": False}


@pytest.mark.asyncio
async def test_inbound_creates_chat_link_and_message(db_engine, tenant, tg_account):
    """Happy path: first message from Telegram creates ChatLink + Message with bitrix_chat_id."""
    from app.database import async_sessionmaker as session_maker_class, AsyncSession, ChatLink, Message

    Session = __import__("sqlalchemy.ext.asyncio", fromlist=["async_sessionmaker"]).async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False
    )

    with patch("app.api.telegram_routes.async_session", Session), \
         patch("app.bitrix.client.send_messages", new_callable=AsyncMock) as mock_send, \
         patch("app.api.telegram_routes.create_or_get_lead", new_callable=AsyncMock) as mock_lead:
        mock_send.return_value = SUCCESS_RESULT
        mock_lead.return_value = {"success": True, "created": True, "id": "lead123"}

        from app.api.telegram_routes import _telegram_inbound_callback
        await _telegram_inbound_callback(
            account_id=tg_account.id,
            chat_id=999888,
            user_id=999888,
            user_name="Alice",
            username="alice",
            text="Hello!",
            message_id=1001,
            recipient="@alice",
        )

    async with Session() as s:
        link = (await s.execute(
            select(ChatLink).where(ChatLink.external_chat_id == "999888")
        )).scalar_one_or_none()
        assert link is not None
        assert link.bitrix_chat_id == "bx99"
        assert link.crm_entity_type == "lead"
        assert link.crm_entity_id == "lead123"

        msg = (await s.execute(
            select(Message).where(Message.idempotency_key == f"telegram_{tg_account.id}_999888_1001_inbound")
        )).scalar_one_or_none()
        assert msg is not None
        assert msg.status == "sent"
        assert msg.text == "Hello!"
        assert msg.sender_name == "Alice"
    mock_lead.assert_called_once()


@pytest.mark.asyncio
async def test_inbound_idempotency_blocks_duplicate(db_engine, tenant, tg_account):
    """Second call with same message_id is silently ignored."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
    from app.database import Message

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    with patch("app.api.telegram_routes.async_session", Session), \
         patch("app.bitrix.client.send_messages", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = SUCCESS_RESULT

        from app.api.telegram_routes import _telegram_inbound_callback
        kwargs = dict(account_id=tg_account.id, chat_id=111, user_id=111, user_name="Bob",
                      username="bob", text="Hi", message_id=5000, recipient="@bob")
        await _telegram_inbound_callback(**kwargs)
        await _telegram_inbound_callback(**kwargs)  # duplicate

    async with Session() as s:
        msgs = (await s.execute(
            select(Message).where(Message.idempotency_key.like(f"telegram_{tg_account.id}_111_5000%"))
        )).scalars().all()
    assert len(msgs) == 1, "Duplicate message should not be inserted"


@pytest.mark.asyncio
async def test_inbound_account_not_found_is_silent(db_engine, tenant):
    """Missing account_id: function returns silently without exception."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    with patch("app.api.telegram_routes.async_session", Session), \
         patch("app.bitrix.client.send_messages", new_callable=AsyncMock) as mock_send:
        from app.api.telegram_routes import _telegram_inbound_callback
        # Should not raise
        await _telegram_inbound_callback(
            account_id=99999,  # does not exist
            chat_id=123, user_id=123, user_name="X", username="x",
            text="test", message_id=1,
        )
    mock_send.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.skip(reason="PostgreSQL foreign keys prevent orphan TelegramAccount rows")
async def test_inbound_tenant_not_found_is_silent(db_engine):
    """Missing tenant: function returns silently."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
    from app.database import TelegramAccount

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    # Insert account with no matching tenant
    async with Session() as s:
        acc = TelegramAccount(tenant_id=9999, api_id=1, api_hash="h", state="active",
                               bitrix_line_id=30, bitrix_connector_id="tg_1")
        s.add(acc)
        await s.commit()
        acc_id = acc.id

    with patch("app.api.telegram_routes.async_session", Session), \
         patch("app.bitrix.client.send_messages", new_callable=AsyncMock) as mock_send:
        from app.api.telegram_routes import _telegram_inbound_callback
        await _telegram_inbound_callback(
            account_id=acc_id, chat_id=5, user_id=5, user_name="Y", username="y",
            text="hi", message_id=2,
        )
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_inbound_existing_chat_link_reused(db_engine, tenant, tg_account, chat_link):
    """If ChatLink already exists, no duplicate is created."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
    from app.database import ChatLink

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    with patch("app.api.telegram_routes.async_session", Session), \
         patch("app.bitrix.client.send_messages", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = SUCCESS_RESULT

        from app.api.telegram_routes import _telegram_inbound_callback
        await _telegram_inbound_callback(
            account_id=tg_account.id,
            chat_id=int(chat_link.external_chat_id),
            user_id=int(chat_link.external_user_id),
            user_name="Test User",
            username="testrecipient",
            text="Repeat message",
            message_id=9999,
        )

    async with Session() as s:
        links = (await s.execute(
            select(ChatLink).where(ChatLink.external_chat_id == chat_link.external_chat_id)
        )).scalars().all()
    assert len(links) == 1, "Only one ChatLink should exist"


@pytest.mark.asyncio
async def test_inbound_same_telegram_chat_id_is_scoped_by_account(db_engine, tenant, tg_account):
    """The same Telegram user id on two connected accounts must create two dialogs."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
    from app.database import ChatLink, Message, TelegramAccount

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as s:
        acc2 = TelegramAccount(
            tenant_id=tenant.id,
            api_id=12345,
            api_hash="testhash2",
            phone="+79007654321",
            username="testuser2",
            session_string="test_session2",
            bitrix_line_id=30,
            bitrix_connector_id="tg_6",
            state="active",
        )
        s.add(acc2)
        await s.commit()
        await s.refresh(acc2)
        acc2_id = acc2.id

    with patch("app.api.telegram_routes.async_session", Session), \
         patch("app.bitrix.client.send_messages", new_callable=AsyncMock) as mock_send, \
         patch("app.api.telegram_routes.create_or_get_lead", new_callable=AsyncMock) as mock_lead:
        mock_send.side_effect = [
            {"SUCCESS": True, "DATA": {"RESULT": [{"session": {"CHAT_ID": "bx_acc1"}}]}},
            {"SUCCESS": True, "DATA": {"RESULT": [{"session": {"CHAT_ID": "bx_acc2"}}]}},
        ]
        mock_lead.return_value = {"success": False, "reason": "skip"}

        from app.api.telegram_routes import _telegram_inbound_callback
        await _telegram_inbound_callback(
            account_id=tg_account.id,
            chat_id=123456,
            user_id=123456,
            user_name="Same User",
            username="sameuser",
            text="to account one",
            message_id=1,
            recipient="@sameuser",
        )
        await _telegram_inbound_callback(
            account_id=acc2_id,
            chat_id=123456,
            user_id=123456,
            user_name="Same User",
            username="sameuser",
            text="to account two",
            message_id=2,
            recipient="@sameuser",
        )

    async with Session() as s:
        links = (await s.execute(
            select(ChatLink).where(ChatLink.external_chat_id == "123456").order_by(ChatLink.account_id.asc())
        )).scalars().all()
        messages = (await s.execute(
            select(Message).where(Message.chat_id_tg == "123456").order_by(Message.account_id.asc())
        )).scalars().all()

    assert len(links) == 2
    assert {link.account_id for link in links} == {tg_account.id, acc2_id}
    assert {link.bitrix_chat_id for link in links} == {"bx_acc1", "bx_acc2"}
    assert {msg.account_id for msg in messages} == {tg_account.id, acc2_id}


@pytest.mark.asyncio
async def test_direct_telegram_outgoing_is_mirrored_to_openline(db_engine, tenant, tg_account, chat_link):
    """Manual outgoing messages from Telegram are mirrored into an existing Open Line."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
    from app.database import Message

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    captured_payload = {}

    async def capture_send(domain, token, connector, line, messages):
        captured_payload.update(messages[0])
        return SUCCESS_RESULT

    with patch("app.api.telegram_routes.async_session", Session), \
         patch("app.bitrix.client.send_messages", new_callable=AsyncMock, side_effect=capture_send):

        from app.api.telegram_routes import _telegram_inbound_callback
        await _telegram_inbound_callback(
            account_id=tg_account.id,
            chat_id=int(chat_link.external_chat_id),
            user_id=int(chat_link.external_user_id),
            user_name="Test User",
            username="testrecipient",
            text="Manual Telegram answer",
            message_id=4242,
            recipient="@testrecipient",
            is_outgoing=True,
        )

    assert captured_payload["message"]["text"] == "[Ответ из Telegram]\nManual Telegram answer"
    assert captured_payload["message"]["disable_crm"] == "Y"
    assert captured_payload["message"]["id"] == f"tg_{tg_account.id}_4242_outgoing"
    assert captured_payload["chat"]["id"] == chat_link.external_chat_id

    async with Session() as s:
        msg = (await s.execute(
            select(Message).where(Message.idempotency_key == f"telegram_{tg_account.id}_{chat_link.external_chat_id}_4242_outgoing")
        )).scalar_one_or_none()
    assert msg is not None
    assert msg.direction == "outbound"
    assert msg.sender_name == "Telegram @testuser"


@pytest.mark.asyncio
async def test_direct_telegram_outgoing_without_link_creates_openline(db_engine, tenant, tg_account, channel):
    """Manual Telegram outgoing messages should create a ChatLink instead of being dropped as no_link."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
    from app.database import ChatLink, Message

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    captured_payload = {}

    async def capture_send(domain, token, connector, line, messages):
        captured_payload.update(messages[0])
        return SUCCESS_RESULT

    with patch("app.api.telegram_routes.async_session", Session), \
         patch("app.bitrix.client.send_messages", new_callable=AsyncMock, side_effect=capture_send):

        from app.api.telegram_routes import _telegram_inbound_callback
        await _telegram_inbound_callback(
            account_id=tg_account.id,
            chat_id=987654,
            user_id=987654,
            user_name="Solo Ranker (@So1oRanker)",
            username="So1oRanker",
            text="Direct Telegram answer",
            message_id=5151,
            recipient="@So1oRanker",
            is_outgoing=True,
        )

    assert captured_payload["message"]["text"] == "[Ответ из Telegram]\nDirect Telegram answer"
    assert captured_payload["message"]["disable_crm"] == "Y"
    assert captured_payload["message"]["id"] == f"tg_{tg_account.id}_5151_outgoing"
    assert captured_payload["user"]["name"] == "Solo Ranker"
    assert captured_payload["user"]["url"] == "https://t.me/So1oRanker"

    async with Session() as s:
        link = (await s.execute(
            select(ChatLink).where(ChatLink.external_chat_id == "987654")
        )).scalar_one_or_none()
        msg = (await s.execute(
            select(Message).where(Message.idempotency_key == f"telegram_{tg_account.id}_987654_5151_outgoing")
        )).scalar_one_or_none()
    assert link is not None
    assert link.bitrix_chat_id == "bx99"
    assert link.channel_id == channel.id
    assert msg is not None
    assert msg.status == "sent"
    assert msg.direction == "outbound"


@pytest.mark.asyncio
async def test_inbound_bitrix_raises_exception_saves_failed(db_engine, tenant, tg_account):
    """When Bitrix24 raises an exception, message is saved with status=failed."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
    from app.database import Message

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    with patch("app.api.telegram_routes.async_session", Session), \
         patch("app.bitrix.client.send_messages", new_callable=AsyncMock) as mock_send:
        mock_send.side_effect = Exception("Bitrix is down")

        from app.api.telegram_routes import _telegram_inbound_callback
        await _telegram_inbound_callback(
            account_id=tg_account.id, chat_id=777, user_id=777, user_name="Z",
            username="z", text="crash test", message_id=7777,
        )

    async with Session() as s:
        msg = (await s.execute(
            select(Message).where(Message.provider_message_id == "7777")
        )).scalar_one_or_none()
    assert msg is not None
    assert msg.status == "failed"


@pytest.mark.asyncio
async def test_inbound_bitrix_success_false_does_not_update_chat_id(db_engine, tenant, tg_account):
    """When Bitrix24 returns SUCCESS=False, bitrix_chat_id is NOT saved and message is failed."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
    from app.database import ChatLink, Message

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    with patch("app.api.telegram_routes.async_session", Session), \
         patch("app.bitrix.client.send_messages", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = FAIL_RESULT

        from app.api.telegram_routes import _telegram_inbound_callback
        await _telegram_inbound_callback(
            account_id=tg_account.id, chat_id=888, user_id=888, user_name="W",
            username="w", text="test", message_id=8888,
        )

    async with Session() as s:
        link = (await s.execute(
            select(ChatLink).where(ChatLink.external_chat_id == "888")
        )).scalar_one_or_none()
        msg = (await s.execute(
            select(Message).where(Message.provider_message_id == "8888")
        )).scalar_one_or_none()
    assert link is not None
    assert link.bitrix_chat_id is None  # not updated on failure
    assert msg is not None
    assert msg.status == "failed"
    assert "SUCCESS=false" in (msg.error or "")


@pytest.mark.asyncio
async def test_inbound_with_media(db_engine, tenant, tg_account, tmp_path):
    """Media bytes are saved to disk and included in Bitrix24 payload."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    fake_media = b"FAKE_JPEG_BYTES"
    captured_payload = {}

    async def capture_send(domain, token, connector, line, messages):
        captured_payload.update(messages[0])
        return SUCCESS_RESULT

    with patch("app.api.telegram_routes.async_session", Session), \
         patch("app.api.telegram_routes.MEDIA_DIR", str(tmp_path)), \
         patch("app.bitrix.client.send_messages", new_callable=AsyncMock, side_effect=capture_send):

        from app.api.telegram_routes import _telegram_inbound_callback
        await _telegram_inbound_callback(
            account_id=tg_account.id, chat_id=555, user_id=555,
            user_name="PhotoSender", username="photosender",
            text="", message_id=5555,
            media_bytes=fake_media, media_name="photo_5555.jpg",
        )

    # File should be saved to tmp_path
    saved_files = list(tmp_path.glob("*.jpg"))
    assert len(saved_files) == 1

    # Payload should include files array
    assert "files" in captured_payload.get("message", {})
    file_entry = captured_payload["message"]["files"][0]
    assert file_entry["type"] == "image"
    assert file_entry["name"] == "photo_5555.jpg"
    assert file_entry["size"] == len(fake_media)


@pytest.mark.asyncio
async def test_inbound_media_save_failure_still_sends_text(db_engine, tenant, tg_account, tmp_path):
    """If media saving fails, the text message is still sent to Bitrix24."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
    from app.database import Message

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    with patch("app.api.telegram_routes.async_session", Session), \
         patch("app.api.telegram_routes.MEDIA_DIR", "/nonexistent/readonly/path"), \
         patch("app.bitrix.client.send_messages", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = SUCCESS_RESULT

        from app.api.telegram_routes import _telegram_inbound_callback
        await _telegram_inbound_callback(
            account_id=tg_account.id, chat_id=444, user_id=444,
            user_name="Sender", username="sender",
            text="text with media", message_id=4444,
            media_bytes=b"bytes", media_name="photo.jpg",
        )

    mock_send.assert_called_once()
    async with Session() as s:
        msg = (await s.execute(
            select(Message).where(Message.provider_message_id == "4444")
        )).scalar_one_or_none()
    assert msg is not None
