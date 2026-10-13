"""
Tests for media (photo/file) support in both directions.
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from io import BytesIO
from types import SimpleNamespace


# ──────────────────────────────────────────────────────────────────────
# _get_media_name (Telethon message media introspection)
# ──────────────────────────────────────────────────────────────────────

def test_get_media_name_photo():
    from app.telegram.service import _get_media_name

    msg = MagicMock()
    msg.photo = MagicMock()
    msg.document = None
    msg.id = 42
    ext, name = _get_media_name(msg)
    assert ext == ".jpg"
    assert name == "photo_42.jpg"


def test_get_media_name_document_with_filename():
    from app.telegram.service import _get_media_name
    from telethon.tl.types import DocumentAttributeFilename

    doc = MagicMock()
    doc.attributes = [DocumentAttributeFilename(file_name="report.pdf")]
    doc.mime_type = "application/pdf"

    msg = MagicMock()
    msg.photo = None
    msg.document = doc
    msg.id = 99
    ext, name = _get_media_name(msg)
    assert ext == ".pdf"
    assert name == "report.pdf"


def test_get_media_name_document_no_filename_image_mime():
    from app.telegram.service import _get_media_name

    doc = MagicMock()
    doc.attributes = []
    doc.mime_type = "image/png"

    msg = MagicMock()
    msg.photo = None
    msg.document = doc
    msg.id = 7
    ext, name = _get_media_name(msg)
    assert ext == ".png"
    assert "image_7" in name


def test_get_media_name_document_audio():
    from app.telegram.service import _get_media_name

    doc = MagicMock()
    doc.attributes = []
    doc.mime_type = "audio/ogg"

    msg = MagicMock()
    msg.photo = None
    msg.document = doc
    msg.id = 3
    ext, name = _get_media_name(msg)
    assert ext == ".ogg"
    assert "audio_3" in name


def test_get_media_name_no_media():
    from app.telegram.service import _get_media_name

    msg = MagicMock()
    msg.photo = None
    msg.document = None
    msg.id = 1
    ext, name = _get_media_name(msg)
    assert ext == ""
    assert name == ""


# ──────────────────────────────────────────────────────────────────────
# _get_bitrix_file_type
# ──────────────────────────────────────────────────────────────────────

def test_get_bitrix_file_type_image():
    from app.api.telegram_routes import _get_bitrix_file_type
    assert _get_bitrix_file_type("photo.jpg") == "image"
    assert _get_bitrix_file_type("image.PNG") == "image"
    assert _get_bitrix_file_type("anim.gif") == "image"
    assert _get_bitrix_file_type("photo.webp") == "image"


def test_get_bitrix_file_type_file():
    from app.api.telegram_routes import _get_bitrix_file_type
    assert _get_bitrix_file_type("document.pdf") == "file"
    assert _get_bitrix_file_type("archive.zip") == "file"
    assert _get_bitrix_file_type("audio.ogg") == "file"
    assert _get_bitrix_file_type("video.mp4") == "file"


# ──────────────────────────────────────────────────────────────────────
# _save_media
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_save_media_creates_file(tmp_path):
    from app.api.telegram_routes import _save_media
    with patch("app.api.telegram_routes.MEDIA_DIR", str(tmp_path)):
        filename = await _save_media(b"IMAGE_BYTES", "test.jpg")
    assert filename.endswith(".jpg")
    saved = tmp_path / filename
    assert saved.exists()
    assert saved.read_bytes() == b"IMAGE_BYTES"


@pytest.mark.asyncio
async def test_save_media_preserves_extension(tmp_path):
    from app.api.telegram_routes import _save_media
    with patch("app.api.telegram_routes.MEDIA_DIR", str(tmp_path)):
        filename = await _save_media(b"PDF_BYTES", "document.pdf")
    assert filename.endswith(".pdf")


@pytest.mark.asyncio
async def test_save_media_unique_filenames(tmp_path):
    from app.api.telegram_routes import _save_media
    with patch("app.api.telegram_routes.MEDIA_DIR", str(tmp_path)):
        f1 = await _save_media(b"A", "photo.jpg")
        f2 = await _save_media(b"B", "photo.jpg")
    assert f1 != f2, "Each save should produce a unique filename"


# ──────────────────────────────────────────────────────────────────────
# Telegram → Bitrix24: media in inbound callback
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_inbound_media_included_in_send_messages(db_engine, tenant, tg_account, tmp_path):
    """Photo bytes → saved locally → files[] included in Bitrix24 payload."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    captured = {}

    async def capture_send(domain, token, connector, line, messages):
        captured["msg"] = messages[0]
        return {"SUCCESS": True, "DATA": {"RESULT": [{"session": {"CHAT_ID": "c1"}}]}}

    with patch("app.api.telegram_routes.async_session", Session), \
         patch("app.api.telegram_routes.MEDIA_DIR", str(tmp_path)), \
         patch("app.bitrix.client.send_messages", new_callable=AsyncMock, side_effect=capture_send):

        from app.api.telegram_routes import _telegram_inbound_callback
        await _telegram_inbound_callback(
            account_id=tg_account.id,
            chat_id=321321,
            user_id=321321,
            user_name="MediaUser",
            username="mediauser",
            text="",
            message_id=321,
            media_bytes=b"JPEG_DATA",
            media_name="photo_321.jpg",
        )

    msg = captured.get("msg", {})
    assert "files" in msg.get("message", {})
    files = msg["message"]["files"]
    assert len(files) == 1
    assert files[0]["type"] == "image"
    assert files[0]["size"] == len(b"JPEG_DATA")
    assert "/media/" in files[0]["link"]


# ──────────────────────────────────────────────────────────────────────
# Bitrix24 → Telegram: file in operator reply
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_to_external_downloads_and_sends_file():
    """File in outbox payload → download → send_file called."""
    fake_bytes = b"FILE_CONTENT_HERE"

    # send_file is imported from app.telegram.service inside _send_to_external_handler
    with patch("app.telegram.service.send_file", new_callable=AsyncMock) as mock_send_file, \
         patch("app.telegram.service.send_by_entity", new_callable=AsyncMock), \
         patch("app.api.routes._download_url_limited", new_callable=AsyncMock) as mock_download:

        mock_send_file.return_value = {"success": True}
        mock_download.return_value = {"success": True, "bytes": fake_bytes, "size": len(fake_bytes)}

        from app.api.routes import _send_to_external_handler
        result = await _send_to_external_handler({
            "account_id": 6,
            "external_chat_id": "111222333",
            "text": "See attachment",
            "files": [{"link": "https://example.com/f.pdf", "name": "report.pdf"}],
        })

    mock_send_file.assert_called_once()
    call_args = mock_send_file.call_args[0]
    assert call_args[2] == fake_bytes
    assert call_args[3] == "report.pdf"


@pytest.mark.asyncio
async def test_send_to_external_file_download_failure_continues():
    """Failed file download (404) → send_file NOT called."""
    with patch("app.telegram.service.send_file", new_callable=AsyncMock) as mock_send_file, \
         patch("app.telegram.service.send_by_entity", new_callable=AsyncMock) as mock_send_text, \
         patch("app.api.routes._download_url_limited", new_callable=AsyncMock) as mock_download:

        mock_send_text.return_value = {"success": True}
        mock_download.return_value = {"success": False, "error": "HTTP 404", "status_code": 404}

        from app.api.routes import _send_to_external_handler
        result = await _send_to_external_handler({
            "account_id": 6,
            "external_chat_id": "999",
            "text": "text",
            "files": [{"link": "https://example.com/missing.pdf", "name": "missing.pdf"}],
        })

    mock_send_file.assert_not_called()  # 404 → file not sent


# ──────────────────────────────────────────────────────────────────────
# send_file in Telegram service
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_service_send_file_success():
    """send_file sends BytesIO to the correct Telegram chat."""
    mock_client = AsyncMock()
    mock_client.send_file = AsyncMock(return_value=MagicMock())

    with patch("app.telegram.service._active_clients", {6: mock_client}):
        from app.telegram.service import send_file
        result = await send_file(6, 111222333, b"IMG_BYTES", "photo.jpg", caption="Look!")

    assert result["success"] is True
    mock_client.send_file.assert_called_once()
    call_args = mock_client.send_file.call_args
    assert call_args[0][0] == 111222333
    assert call_args.kwargs["force_document"] is False
    assert call_args.kwargs["attributes"] is None


@pytest.mark.asyncio
async def test_service_send_png_as_photo_not_document():
    """PNG from Bitrix24 must be sent to Telegram as photo, not document."""
    mock_client = AsyncMock()
    mock_client.send_file = AsyncMock(return_value=MagicMock())

    with patch("app.telegram.service._active_clients", {6: mock_client}):
        from app.telegram.service import send_file
        result = await send_file(6, 111222333, b"PNG_BYTES", "image.png", caption="")

    assert result["success"] is True
    call_args = mock_client.send_file.call_args
    assert call_args.kwargs["force_document"] is False
    assert call_args.kwargs["attributes"] is None


@pytest.mark.asyncio
async def test_service_send_pdf_as_document():
    """Non-image files should still be sent to Telegram as documents."""
    mock_client = AsyncMock()
    mock_client.send_file = AsyncMock(return_value=MagicMock())

    with patch("app.telegram.service._active_clients", {6: mock_client}):
        from app.telegram.service import send_file
        result = await send_file(6, 111222333, b"PDF_BYTES", "report.pdf", caption="")

    assert result["success"] is True
    call_args = mock_client.send_file.call_args
    assert call_args.kwargs["force_document"] is True
    assert call_args.kwargs["attributes"]


@pytest.mark.asyncio
async def test_service_send_file_account_not_active():
    """send_file returns error when account not in _active_clients."""
    with patch("app.telegram.service._active_clients", {}):
        from app.telegram.service import send_file
        result = await send_file(99, 123, b"bytes", "f.jpg")

    assert result["success"] is False
    assert "not active" in result["error"]


@pytest.mark.asyncio
async def test_service_send_file_telethon_error():
    """send_file returns error dict when Telethon raises."""
    mock_client = AsyncMock()
    mock_client.send_file = AsyncMock(side_effect=Exception("Telethon error"))

    with patch("app.telegram.service._active_clients", {6: mock_client}):
        from app.telegram.service import send_file
        result = await send_file(6, 123, b"bytes", "f.jpg")

    assert result["success"] is False
    assert "Telethon error" in result["error"]


@pytest.mark.asyncio
async def test_service_send_by_entity_retries_after_entity_cache_miss():
    """Numeric Telegram ids can need dialog cache warmup after restart."""
    entity = SimpleNamespace(id=6580601543)

    async def iter_dialogs(limit=500):
        yield SimpleNamespace(entity=entity)

    sent_message = MagicMock()
    sent_message.id = 42
    sent_message.peer_id = None
    mock_client = MagicMock()
    mock_client.send_message = AsyncMock(side_effect=[
        Exception("Could not find the input entity for PeerUser(user_id=6580601543)"),
        sent_message,
    ])
    mock_client.iter_dialogs = iter_dialogs

    with patch("app.telegram.service._active_clients", {6: mock_client}):
        from app.telegram.service import send_by_entity
        result = await send_by_entity(6, 6580601543, "hello")

    assert result["success"] is True
    assert result["message_id"] == 42
    assert mock_client.send_message.call_args_list[1].args[0] is entity
