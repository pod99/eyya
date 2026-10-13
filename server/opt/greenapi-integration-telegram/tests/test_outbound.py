"""
Tests for OutboxQueue and _send_to_external_handler — all edge cases.
"""
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
from app.database import utcnow


# ──────────────────────────────────────────────────────────────────────
# OutboxQueue tests
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_outbox_enqueue_and_process_success(db_engine):
    """Enqueue → process_next → entry becomes 'sent'."""
    from app.outbox import OutboxQueue, OutboxMessage
    from app.database import OutboxEntry

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    q = OutboxQueue()

    async def handler(payload):
        return {"success": True}

    q.register_handler("to_external", handler)

    # Outbox imports async_session from app.database at call time, so we patch there
    with patch("app.database.async_session", Session):
        msg = OutboxMessage(direction="to_external", payload={"text": "hello"})
        entry_id = await q.enqueue(msg)
        assert entry_id is not None
        processed = await q.process_next()
        assert processed is True

    async with Session() as s:
        entry = (await s.execute(select(OutboxEntry).where(OutboxEntry.id == entry_id))).scalar_one()
    assert entry.status == "sent"
    assert entry.attempts == 1


@pytest.mark.asyncio
async def test_outbox_process_next_empty_queue(db_engine):
    """process_next returns False when queue is empty."""
    from app.outbox import OutboxQueue

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    q = OutboxQueue()

    with patch("app.database.async_session", Session):
        result = await q.process_next()
    assert result is False


@pytest.mark.asyncio
async def test_outbox_retry_on_handler_exception(db_engine):
    """Handler raises → entry stays pending with attempts incremented."""
    from app.outbox import OutboxQueue, OutboxMessage
    from app.database import OutboxEntry

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    q = OutboxQueue()

    async def flaky_handler(payload):
        raise Exception("temporary error")

    q.register_handler("to_external", flaky_handler)

    with patch("app.database.async_session", Session):
        entry_id = await q.enqueue(OutboxMessage(direction="to_external", payload={}, max_retries=3))
        await q.process_next()

    async with Session() as s:
        entry = (await s.execute(select(OutboxEntry).where(OutboxEntry.id == entry_id))).scalar_one()
    assert entry.status == "pending"  # will retry
    assert entry.attempts == 1
    assert entry.last_error == "temporary error"


@pytest.mark.asyncio
async def test_outbox_max_retries_leads_to_failed(db_engine):
    """After max_retries, entry becomes 'failed'."""
    from app.outbox import OutboxQueue, OutboxMessage
    from app.database import OutboxEntry

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    q = OutboxQueue()

    async def always_fails(payload):
        raise Exception("always")

    q.register_handler("to_external", always_fails)

    with patch("app.database.async_session", Session):
        entry_id = await q.enqueue(OutboxMessage(direction="to_external", payload={}, max_retries=1))
        # max_retries=1 means it fails permanently on first attempt
        await q.process_next()

    async with Session() as s:
        entry = (await s.execute(select(OutboxEntry).where(OutboxEntry.id == entry_id))).scalar_one()
    assert entry.status == "failed"


@pytest.mark.asyncio
async def test_outbox_no_handler_leads_to_failed(db_engine):
    """No handler registered → entry immediately fails."""
    from app.outbox import OutboxQueue, OutboxMessage
    from app.database import OutboxEntry

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    q = OutboxQueue()  # no handlers registered

    with patch("app.database.async_session", Session):
        entry_id = await q.enqueue(OutboxMessage(direction="unknown_direction", payload={}))
        await q.process_next()

    async with Session() as s:
        entry = (await s.execute(select(OutboxEntry).where(OutboxEntry.id == entry_id))).scalar_one()
    assert entry.status == "failed"
    assert "No handler" in entry.last_error


@pytest.mark.asyncio
async def test_outbox_retry_entry_resets_failed(db_engine):
    """retry_entry() sets status=pending, attempts=0 for a failed entry."""
    from app.outbox import OutboxQueue
    from app.database import OutboxEntry

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as s:
        entry = OutboxEntry(
            direction="to_external",
            payload_json='{"text": "x"}',
            status="failed",
            attempts=3,
            last_error="boom",
            next_retry_at=utcnow(),
        )
        s.add(entry)
        await s.commit()
        entry_id = entry.id

    q = OutboxQueue()
    with patch("app.database.async_session", Session):
        success = await q.retry_entry(entry_id)

    assert success is True
    async with Session() as s:
        entry = (await s.execute(select(OutboxEntry).where(OutboxEntry.id == entry_id))).scalar_one()
    assert entry.status == "pending"
    assert entry.attempts == 0
    assert entry.last_error is None


@pytest.mark.asyncio
async def test_outbox_retry_entry_ignores_non_failed(db_engine):
    """retry_entry() returns False for entries not in 'failed' state."""
    from app.outbox import OutboxQueue
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

    q = OutboxQueue()
    with patch("app.database.async_session", Session):
        result = await q.retry_entry(entry_id)
    assert result is False


@pytest.mark.asyncio
async def test_outbox_get_stats(db_engine):
    """get_stats() returns counts per status."""
    from app.outbox import OutboxQueue
    from app.database import OutboxEntry

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as s:
        for status, count in [("pending", 2), ("sent", 3), ("failed", 1)]:
            for _ in range(count):
                s.add(OutboxEntry(
                    direction="to_external", payload_json="{}",
                    status=status, next_retry_at=utcnow()
                ))
        await s.commit()

    q = OutboxQueue()
    with patch("app.database.async_session", Session):
        stats = await q.get_stats()

    assert stats["pending"] == 2
    assert stats["sent"] == 3
    assert stats["failed"] == 1
    assert stats["total"] == 6


# ──────────────────────────────────────────────────────────────────────
# _send_to_external_handler tests
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_to_external_telegram_text():
    """account_id present → calls send_by_entity with correct args."""
    # In routes.py, send_by_entity is imported inside the function from app.telegram.service
    with patch("app.telegram.service.send_by_entity", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = {"success": True}

        from app.api.routes import _send_to_external_handler
        result = await _send_to_external_handler({
            "account_id": 6,
            "external_chat_id": "111222333",
            "text": "Hello from Bitrix!",
            "files": [],
        })

    assert result["success"] is True
    mock_send.assert_called_once_with(6, 111222333, "Hello from Bitrix!")


@pytest.mark.asyncio
async def test_send_to_external_no_account_uses_http_provider():
    """No account_id → falls through to HTTP provider."""
    mock_provider = MagicMock()
    mock_provider.send_message = AsyncMock(return_value={"success": True})

    with patch("app.api.routes.get_provider", return_value=mock_provider):
        from app.api.routes import _send_to_external_handler
        result = await _send_to_external_handler({
            "external_chat_id": "chat123",
            "text": "via HTTP",
            "provider_key": "whatsapp_1",
            "channel_id": 1,
            "files": [],
        })

    assert result["success"] is True
    mock_provider.send_message.assert_called_once()


@pytest.mark.asyncio
async def test_send_to_external_no_provider_returns_error():
    """No account_id and no matching HTTP provider → returns error."""
    with patch("app.api.routes.get_provider", return_value=None):
        from app.api.routes import _send_to_external_handler
        result = await _send_to_external_handler({
            "external_chat_id": "chat123",
            "text": "test",
            "provider_key": "nonexistent",
            "channel_id": 1,
            "files": [],
        })
    assert result["success"] is False
    assert "No provider" in result["error"]


@pytest.mark.asyncio
async def test_send_to_external_with_file():
    """account_id + files → downloads and calls send_file."""
    fake_file_bytes = b"PDF_CONTENT"

    # send_file and send_by_entity are imported at call time from app.telegram.service
    with patch("app.telegram.service.send_file", new_callable=AsyncMock) as mock_send_file, \
         patch("app.telegram.service.send_by_entity", new_callable=AsyncMock) as mock_send_text, \
         patch("app.api.routes._download_url_limited", new_callable=AsyncMock) as mock_download:

        mock_send_file.return_value = {"success": True}
        mock_download.return_value = {"success": True, "bytes": fake_file_bytes, "size": len(fake_file_bytes)}

        from app.api.routes import _send_to_external_handler
        result = await _send_to_external_handler({
            "account_id": 6,
            "external_chat_id": "111222333",
            "text": "",
            "files": [{"link": "https://bitrix24.example.com/file.pdf", "name": "doc.pdf"}],
        })

    mock_send_file.assert_called_once()
    call_args = mock_send_file.call_args[0]
    assert call_args[0] == 6     # account_id
    assert call_args[2] == fake_file_bytes  # file_bytes
    assert call_args[3] == "doc.pdf"        # filename
