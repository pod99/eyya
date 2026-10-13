import logging
import re

from app.bitrix import client as bx
from app.crypto import decrypt_token

logger = logging.getLogger(__name__)

LEAD_ORIGINATOR_ID = "SAFY_TELEGRAM"
LEAD_STATUS_NEW = "NEW"
OPENLINE_NOTE_MARKER = "SAFY_OPENLINE_CHAT"


def lead_origin_id(connector_id: str, external_chat_id) -> str:
    return f"{connector_id}:{external_chat_id}"


def _lead_person_name(display_name: str = "", recipient: str = "") -> str:
    raw = re.sub(r"\(@[^)]*\)", "", display_name or "").strip()
    raw = raw or (recipient or "").lstrip("@+") or "Telegram user"
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw[:100] or "Telegram user"


def _lead_title(display_name: str = "", recipient: str = "") -> str:
    name = _lead_person_name(display_name, recipient)
    if recipient and recipient not in name:
        return f"Telegram: {name} ({recipient})"
    return f"Telegram: {name}"


def _lead_fields(connector_id: str, external_chat_id, display_name: str = "",
                 recipient: str = "", first_text: str = "",
                 bitrix_chat_id=None, assigned_by_id=None) -> dict:
    name = _lead_person_name(display_name, recipient)
    comments = [
        "Создано автоматически из Telegram/Open Lines.",
        f"Telegram chat id: {external_chat_id}",
    ]
    if recipient:
        comments.append(f"Telegram: {recipient}")
    if bitrix_chat_id:
        comments.append(f"Open Line chat id: {bitrix_chat_id}")
    if first_text:
        comments.append("")
        comments.append("Первое сообщение:")
        comments.append(first_text[:3000])

    fields = {
        "TITLE": _lead_title(display_name, recipient),
        "NAME": name,
        "STATUS_ID": LEAD_STATUS_NEW,
        "OPENED": "Y",
        "ORIGINATOR_ID": LEAD_ORIGINATOR_ID,
        "ORIGIN_ID": lead_origin_id(connector_id, external_chat_id),
        "COMMENTS": "\n".join(comments),
    }
    if assigned_by_id:
        try:
            fields["ASSIGNED_BY_ID"] = int(assigned_by_id)
        except (TypeError, ValueError):
            pass
    if recipient and recipient.startswith("+"):
        fields["PHONE"] = [{"VALUE": recipient, "VALUE_TYPE": "WORK"}]
    elif recipient and recipient.startswith("@"):
        fields["IM"] = [{"VALUE": recipient, "VALUE_TYPE": "TELEGRAM"}]
    return fields


def openline_chat_url(domain: str, bitrix_chat_id) -> str:
    return f"https://{domain}/online/?IM_DIALOG=chat{bitrix_chat_id}"


def _openline_note_marker(bitrix_chat_id) -> str:
    return f"{OPENLINE_NOTE_MARKER}:{bitrix_chat_id}"


def _openline_note_comment(domain: str, bitrix_chat_id, display_name: str = "",
                           recipient: str = "", first_text: str = "") -> str:
    marker = _openline_note_marker(bitrix_chat_id)
    chat_url = openline_chat_url(domain, bitrix_chat_id)
    title = display_name or recipient or "Telegram client"
    lines = [
        "[B]Чат с клиентом в открытой линии[/B]",
        f"Клиент: {title}",
        f"Open Line chat id: {bitrix_chat_id}",
        f"[URL={chat_url}]Открыть чат и продолжить переписку[/URL]",
    ]
    if first_text:
        lines.extend(["", "[B]Первое сообщение[/B]", first_text[:1000]])
    lines.extend(["", marker])
    return "\n".join(lines)


def lead_url(domain: str, lead_id) -> str:
    return f"https://{domain}/crm/lead/details/{lead_id}/"


def _extract_lead_id(result) -> str | None:
    if isinstance(result, int):
        return str(result)
    if isinstance(result, str) and result.isdigit():
        return result
    if isinstance(result, dict):
        for key in ("ID", "id", "LEAD_ID", "lead_id", "CRM_ENTITY", "ENTITY_ID"):
            value = result.get(key)
            if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
                return str(value)
        for value in result.values():
            nested = _extract_lead_id(value)
            if nested:
                return nested
    return None


async def _timeline_note_exists(domain: str, token: str, lead_id: str, bitrix_chat_id) -> bool:
    marker = _openline_note_marker(bitrix_chat_id)
    result = await bx._call(domain, token, "crm.timeline.comment.list", {
        "filter": {"ENTITY_ID": int(lead_id), "ENTITY_TYPE": "lead"},
        "select": ["ID", "COMMENT"],
        "order": {"ID": "DESC"},
        "start": 0,
    })
    if not isinstance(result, list):
        return False
    return any(marker in str(item.get("COMMENT") or "") for item in result if isinstance(item, dict))


async def ensure_openline_timeline_note(domain: str, token_enc: str, lead_id: str,
                                        bitrix_chat_id=None, display_name: str = "",
                                        recipient: str = "", first_text: str = "") -> dict:
    if not (lead_id and bitrix_chat_id):
        return {"success": False, "skipped": True, "reason": "missing lead_id or bitrix_chat_id"}

    token = decrypt_token(token_enc)
    try:
        if await _timeline_note_exists(domain, token, str(lead_id), bitrix_chat_id):
            return {"success": True, "created": False, "exists": True}
    except Exception as exc:
        logger.warning("Could not check lead timeline note lead=%s chat=%s: %s", lead_id, bitrix_chat_id, exc)

    comment = _openline_note_comment(
        domain=domain,
        bitrix_chat_id=bitrix_chat_id,
        display_name=display_name,
        recipient=recipient,
        first_text=first_text,
    )
    result = await bx._call(domain, token, "crm.timeline.comment.add", {
        "fields": {
            "ENTITY_ID": int(lead_id),
            "ENTITY_TYPE": "lead",
            "COMMENT": comment,
        }
    })
    if result:
        return {"success": True, "created": True, "id": str(result), "url": openline_chat_url(domain, bitrix_chat_id)}
    return {"success": False, "created": False, "id": None}


async def get_openline_chats_for_lead(domain: str, token_enc: str, lead_id: str) -> list:
    token = decrypt_token(token_enc)
    result = await bx._call(domain, token, "imopenlines.crm.chat.get", {
        "CRM_ENTITY_TYPE": "lead",
        "CRM_ENTITY": int(lead_id),
        "ACTIVE_ONLY": "N",
    }, timeout=15)
    return result if isinstance(result, list) else []


async def create_native_openline_lead(domain: str, token_enc: str, bitrix_chat_id,
                                      connector_id: str, external_chat_id,
                                      display_name: str = "", recipient: str = "",
                                      first_text: str = "", assigned_by_id=None) -> dict:
    if not bitrix_chat_id:
        return {"success": False, "native": True, "error": "missing bitrix_chat_id"}

    token = decrypt_token(token_enc)
    result = await bx._call(domain, token, "imopenlines.crm.lead.create", {
        "CHAT_ID": int(bitrix_chat_id),
    }, timeout=20)
    lead_id = _extract_lead_id(result)
    if not lead_id:
        return {
            "success": False,
            "native": True,
            "created": bool(result),
            "id": None,
            "result": result,
            "error": "Bitrix did not return lead id",
        }

    fields = _lead_fields(
        connector_id=connector_id,
        external_chat_id=external_chat_id,
        display_name=display_name,
        recipient=recipient,
        first_text=first_text,
        bitrix_chat_id=bitrix_chat_id,
        assigned_by_id=assigned_by_id,
    )
    await bx._call(domain, token, "crm.lead.update", {
        "id": int(lead_id),
        "fields": fields,
        "params": {"REGISTER_SONET_EVENT": "Y"},
    })
    note = await ensure_openline_timeline_note(
        domain, token_enc, lead_id, bitrix_chat_id,
        display_name=display_name, recipient=recipient, first_text=first_text,
    )
    return {
        "success": True,
        "native": True,
        "created": True,
        "id": lead_id,
        "result": result,
        "timeline_note": note,
    }


async def find_lead_by_origin(domain: str, token_enc: str, connector_id: str, external_chat_id):
    token = decrypt_token(token_enc)
    result = await bx._call(domain, token, "crm.lead.list", {
        "order": {"ID": "DESC"},
        "filter": {
            "=ORIGINATOR_ID": LEAD_ORIGINATOR_ID,
            "=ORIGIN_ID": lead_origin_id(connector_id, external_chat_id),
        },
        "select": ["ID", "TITLE", "STATUS_ID"],
    })
    if isinstance(result, list) and result:
        return result[0]
    return None


async def create_or_get_lead(domain: str, token_enc: str, connector_id: str, external_chat_id,
                             display_name: str = "", recipient: str = "",
                             first_text: str = "", bitrix_chat_id=None,
                             assigned_by_id=None) -> dict:
    existing = await find_lead_by_origin(domain, token_enc, connector_id, external_chat_id)
    if existing and existing.get("ID"):
        note = await ensure_openline_timeline_note(
            domain, token_enc, str(existing["ID"]), bitrix_chat_id,
            display_name=display_name, recipient=recipient, first_text=first_text,
        )
        return {"success": True, "created": False, "id": str(existing["ID"]), "result": existing, "timeline_note": note}

    token = decrypt_token(token_enc)
    fields = _lead_fields(
        connector_id=connector_id,
        external_chat_id=external_chat_id,
        display_name=display_name,
        recipient=recipient,
        first_text=first_text,
        bitrix_chat_id=bitrix_chat_id,
        assigned_by_id=assigned_by_id,
    )
    result = await bx._call(domain, token, "crm.lead.add", {
        "fields": fields,
        "params": {"REGISTER_SONET_EVENT": "Y"},
    })
    if result:
        note = await ensure_openline_timeline_note(
            domain, token_enc, str(result), bitrix_chat_id,
            display_name=display_name, recipient=recipient, first_text=first_text,
        )
        return {"success": True, "created": True, "id": str(result), "result": result, "timeline_note": note}

    # Some portals validate multifields strictly; retry with only core fields.
    fallback_fields = dict(fields)
    fallback_fields.pop("IM", None)
    fallback_fields.pop("PHONE", None)
    result = await bx._call(domain, token, "crm.lead.add", {
        "fields": fallback_fields,
        "params": {"REGISTER_SONET_EVENT": "Y"},
    })
    if result:
        note = await ensure_openline_timeline_note(
            domain, token_enc, str(result), bitrix_chat_id,
            display_name=display_name, recipient=recipient, first_text=first_text,
        )
        return {"success": True, "created": True, "id": str(result), "result": result, "timeline_note": note}

    logger.warning(
        "Could not create lead for connector=%s chat=%s recipient=%s",
        connector_id, external_chat_id, recipient,
    )
    return {"success": False, "created": False, "id": None, "result": None}
