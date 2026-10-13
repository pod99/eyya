import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_ensure_openline_timeline_note_adds_comment():
    from app.bitrix.crm import ensure_openline_timeline_note, OPENLINE_NOTE_MARKER

    calls = []

    async def fake_call(domain, token, method, params):
        calls.append((method, params))
        if method == "crm.timeline.comment.list":
            return []
        if method == "crm.timeline.comment.add":
            return 777
        return None

    with patch("app.bitrix.crm.decrypt_token", return_value="token"), \
         patch("app.bitrix.crm.bx._call", new=AsyncMock(side_effect=fake_call)):
        result = await ensure_openline_timeline_note(
            "example.bitrix24.ru",
            "encrypted",
            "123",
            "456",
            display_name="Анна",
            recipient="@anna",
            first_text="Здравствуйте",
        )

    add_call = calls[1]
    comment = add_call[1]["fields"]["COMMENT"]
    assert result["success"] is True
    assert result["created"] is True
    assert result["url"] == "https://example.bitrix24.ru/online/?IM_DIALOG=chat456"
    assert add_call[0] == "crm.timeline.comment.add"
    assert "Открыть чат" in comment
    assert f"{OPENLINE_NOTE_MARKER}:456" in comment


@pytest.mark.asyncio
async def test_ensure_openline_timeline_note_is_idempotent():
    from app.bitrix.crm import ensure_openline_timeline_note, OPENLINE_NOTE_MARKER

    mock_call = AsyncMock(return_value=[{"ID": "1", "COMMENT": f"{OPENLINE_NOTE_MARKER}:456"}])

    with patch("app.bitrix.crm.decrypt_token", return_value="token"), \
         patch("app.bitrix.crm.bx._call", mock_call):
        result = await ensure_openline_timeline_note(
            "example.bitrix24.ru",
            "encrypted",
            "123",
            "456",
        )

    assert result["success"] is True
    assert result["created"] is False
    assert mock_call.await_count == 1


@pytest.mark.asyncio
async def test_create_or_get_lead_adds_note_for_existing_lead():
    from app.bitrix.crm import create_or_get_lead

    with patch("app.bitrix.crm.find_lead_by_origin", new=AsyncMock(return_value={"ID": "123"})), \
         patch("app.bitrix.crm.ensure_openline_timeline_note", new=AsyncMock(return_value={"success": True, "created": True})) as note:
        result = await create_or_get_lead(
            "example.bitrix24.ru",
            "encrypted",
            "tg_6",
            "111",
            display_name="Анна",
            recipient="@anna",
            first_text="Привет",
            bitrix_chat_id="456",
        )

    assert result["success"] is True
    assert result["created"] is False
    assert result["timeline_note"]["created"] is True
    note.assert_awaited_once()
