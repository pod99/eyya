import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
from unittest.mock import AsyncMock, patch


class JsonRequest:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


def _fake_bitrix_health(events):
    return {
        "api_ok": True,
        "errors": [],
        "connector_status": {
            "CONNECTOR": "tg_6",
            "LINE": 30,
            "CONFIGURED": True,
            "STATUS": True,
        },
        "events": {
            "expected_handler": "REPLACE_WITH_PRIVATE_WEBHOOK_URLbitrix/events",
            "items": {
                event: {"bound": True, "handler_ok": event in events, "handlers": []}
                for event in events
            },
            "raw_count": len(events),
            "call_ok": True,
        },
        "open_line": {
            "id": 30,
            "found": True,
            "name": "Open Line 15",
            "active": "Y",
            "call_ok": True,
        },
    }


@pytest.mark.asyncio
async def test_admin_health_reports_ok_chain(db_engine, tenant, tg_account, channel, chat_link):
    from app.api.admin import EXPECTED_BITRIX_EVENTS, admin_health

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    fake_bitrix = _fake_bitrix_health(set(EXPECTED_BITRIX_EVENTS))

    with patch("app.api.admin.async_session", Session), \
         patch("app.api.admin._collect_bitrix_health", new=AsyncMock(return_value=fake_bitrix)), \
         patch("app.telegram.service._active_clients", {tg_account.id: object()}):
        result = await admin_health(domain=tenant.bitrix_domain)

    assert result["success"] is True
    assert result["overall"] == "ok"
    assert result["primary"]["line_id"] == 30
    assert result["telegram_accounts"][0]["is_listening"] is True
    assert any(item["code"] == "events" and item["status"] == "ok" for item in result["checks"])


@pytest.mark.asyncio
async def test_admin_health_warns_when_event_handler_missing(db_engine, tenant, tg_account, channel, chat_link):
    from app.api.admin import EXPECTED_BITRIX_EVENTS, admin_health

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    fake_bitrix = _fake_bitrix_health(set(EXPECTED_BITRIX_EVENTS[:-1]))

    with patch("app.api.admin.async_session", Session), \
         patch("app.api.admin._collect_bitrix_health", new=AsyncMock(return_value=fake_bitrix)), \
         patch("app.telegram.service._active_clients", {tg_account.id: object()}):
        result = await admin_health(domain=tenant.bitrix_domain)

    events_check = next(item for item in result["checks"] if item["code"] == "events")
    assert result["overall"] == "warn"
    assert events_check["status"] == "warn"


@pytest.mark.asyncio
async def test_retry_failed_outbox_resets_failed_entries(db_engine):
    from app.api.admin import admin_retry_failed_outbox
    from app.database import OutboxEntry
    from sqlalchemy import select

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as s:
        failed = OutboxEntry(direction="to_external", payload_json="{}", status="failed",
                             attempts=3, last_error="bad")
        skipped = OutboxEntry(direction="to_external", payload_json="{}", status="skipped",
                              attempts=3, last_error="old")
        s.add_all([failed, skipped])
        await s.commit()
        failed_id = failed.id
        skipped_id = skipped.id

    with patch("app.api.admin.async_session", Session):
        result = await admin_retry_failed_outbox(JsonRequest({"limit": 10}))

    async with Session() as s:
        failed = await s.get(OutboxEntry, failed_id)
        skipped = await s.get(OutboxEntry, skipped_id)

    assert result["success"] is True
    assert result["ids"] == [failed_id]
    assert failed.status == "pending"
    assert failed.attempts == 0
    assert failed.last_error is None
    assert skipped.status == "skipped"


@pytest.mark.asyncio
async def test_skip_outbox_marks_failed_entry_skipped(db_engine):
    from app.api.admin import admin_skip_outbox
    from app.database import OutboxEntry

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as s:
        entry = OutboxEntry(direction="to_external", payload_json="{}", status="failed",
                            attempts=3, last_error="permanent")
        s.add(entry)
        await s.commit()
        entry_id = entry.id

    with patch("app.api.admin.async_session", Session):
        result = await admin_skip_outbox(entry_id)

    async with Session() as s:
        entry = await s.get(OutboxEntry, entry_id)

    assert result["success"] is True
    assert entry.status == "skipped"
    assert "Skipped by admin" in entry.last_error


@pytest.mark.asyncio
async def test_cleanup_empty_links_keeps_links_with_messages(db_engine, tenant):
    from app.api.admin import admin_cleanup_empty_links
    from app.database import ChatLink, Message
    from sqlalchemy import select

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as s:
        empty = ChatLink(tenant_id=tenant.id, provider="tg_6", external_chat_id="empty",
                         bitrix_line_id=30)
        with_message = ChatLink(tenant_id=tenant.id, provider="tg_6", external_chat_id="busy",
                                bitrix_line_id=30)
        s.add_all([empty, with_message])
        await s.flush()
        s.add(Message(tenant_id=tenant.id, chat_link_id=with_message.id, direction="inbound",
                      text="hello", status="sent"))
        await s.commit()
        empty_id = empty.id
        busy_id = with_message.id

    with patch("app.api.admin.async_session", Session):
        result = await admin_cleanup_empty_links(JsonRequest({"domain": tenant.bitrix_domain}))

    async with Session() as s:
        remaining_ids = [row_id for (row_id,) in (await s.execute(select(ChatLink.id))).all()]

    assert result["success"] is True
    assert result["deleted"] == 1
    assert empty_id not in remaining_ids
    assert busy_id in remaining_ids


@pytest.mark.asyncio
async def test_leads_audit_reports_dialogs_without_leads(db_engine, tenant, channel, chat_link):
    from app.api.admin import admin_leads_audit

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    with patch("app.api.admin.async_session", Session):
        result = await admin_leads_audit(domain=tenant.bitrix_domain, status="can_create")

    assert result["success"] is True
    assert result["totals"]["total"] >= 1
    assert result["totals"]["can_create"] >= 1
    assert result["rows"][0]["can_create_lead"] is True
    assert result["rows"][0]["chat_url"].endswith("/online/?IM_DIALOG=chatbx_chat_42")


@pytest.mark.asyncio
async def test_create_lead_for_dialog_updates_chat_link(db_engine, tenant, channel, chat_link):
    from app.api.admin import admin_create_lead_for_dialog
    from app.database import ChatLink

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    with patch("app.api.admin.async_session", Session), \
         patch("app.api.admin.create_or_get_lead", new=AsyncMock(return_value={
             "success": True,
             "created": True,
             "id": "555",
             "timeline_note": {"success": True, "created": True},
         })):
        result = await admin_create_lead_for_dialog(JsonRequest({
            "domain": tenant.bitrix_domain,
            "chat_link_id": chat_link.id,
        }))

    async with Session() as s:
        link = await s.get(ChatLink, chat_link.id)

    assert result["success"] is True
    assert result["id"] == "555"
    assert link.crm_entity_type == "lead"
    assert link.crm_entity_id == "555"


@pytest.mark.asyncio
async def test_add_openline_note_for_existing_lead(db_engine, tenant, channel, chat_link):
    from app.api.admin import admin_add_openline_note
    from app.database import ChatLink

    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as s:
        link = await s.get(ChatLink, chat_link.id)
        link.crm_entity_type = "lead"
        link.crm_entity_id = "555"
        await s.commit()

    with patch("app.api.admin.async_session", Session), \
         patch("app.api.admin.ensure_openline_timeline_note", new=AsyncMock(return_value={
             "success": True,
             "created": False,
             "exists": True,
         })):
        result = await admin_add_openline_note(JsonRequest({
            "domain": tenant.bitrix_domain,
            "chat_link_id": chat_link.id,
        }))

    assert result["success"] is True
    assert result["result"]["exists"] is True
