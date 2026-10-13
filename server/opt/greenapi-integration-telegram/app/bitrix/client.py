import base64
import logging
import time
import httpx
from app.config import settings
from app.crypto import decrypt_token

logger = logging.getLogger(__name__)

_app_disk_folder_ids: dict[str, tuple[int, float]] = {}


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "y", "yes", "true", "ok"}
    return False


def send_messages_status(result) -> dict:
    """Normalize imconnector.send.messages result into success/chat/error fields."""
    if not isinstance(result, dict):
        return {"success": False, "chat_id": "", "dialog_id": "", "error": "empty Bitrix24 response"}

    for key in ("error_description", "ERROR_DESCRIPTION", "error", "ERROR"):
        value = result.get(key)
        if value:
            return {"success": False, "chat_id": "", "dialog_id": "", "error": str(value)}

    if "SUCCESS" in result and not _truthy(result.get("SUCCESS")):
        return {"success": False, "chat_id": "", "dialog_id": "", "error": "Bitrix24 returned SUCCESS=false"}
    if "success" in result and not _truthy(result.get("success")):
        return {"success": False, "chat_id": "", "dialog_id": "", "error": "Bitrix24 returned success=false"}

    data = result.get("DATA") if isinstance(result.get("DATA"), dict) else result.get("data")
    if not isinstance(data, dict):
        data = {}
    rows = data.get("RESULT") or data.get("result") or result.get("RESULT") or result.get("result") or []
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        rows = []

    chat_id = ""
    dialog_id = ""
    row_errors = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in ("error_description", "ERROR_DESCRIPTION", "error", "ERROR"):
            value = row.get(key)
            if value:
                row_errors.append(str(value))
        if "SUCCESS" in row and not _truthy(row.get("SUCCESS")):
            row_errors.append("message result SUCCESS=false")
        session = row.get("session") if isinstance(row.get("session"), dict) else row.get("SESSION")
        if isinstance(session, dict):
            chat_id = str(session.get("CHAT_ID") or session.get("chat_id") or chat_id or "")
            dialog_id = str(session.get("DIALOG_ID") or session.get("dialog_id") or dialog_id or "")

    if row_errors:
        return {"success": False, "chat_id": chat_id, "dialog_id": dialog_id, "error": "; ".join(row_errors[:3])}

    success = _truthy(result.get("SUCCESS")) or _truthy(result.get("success")) or bool(rows)
    return {"success": success, "chat_id": chat_id, "dialog_id": dialog_id, "error": "" if success else "Bitrix24 did not confirm delivery"}


async def _call(domain: str, access_token: str, method: str, params: dict = None, timeout: int = 30):
    url = f"https://{domain}/rest/{method}"
    payload = dict(params or {})
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=timeout,
        )
        data = resp.json()
        if data.get("error"):
            err = data.get("error_description", data["error"])
            logger.error(f"Bitrix24 {method}: {err}")
            return None
        return data.get("result")


def _get_token(access_token_encrypted: str) -> str:
    return decrypt_token(access_token_encrypted)


async def register_connector(domain: str, token_enc: str, connector_id: str, connector_name: str, placement_handler: str):
    token = _get_token(token_enc)
    svg = "data:image/svg+xml;charset=US-ASCII,%3Csvg%20xmlns%3D%22http%3A//www.w3.org/2000/svg%22%20viewBox%3D%220%200%2070%2071%22%3E%3Cpath%20fill%3D%22%230088cc%22%20d%3D%22M34.7%2C64c-11.6%2C0-22-7.1-26.3-17.8C4%2C35.4%2C6.4%2C23%2C14.5%2C14.7c8.1-8.2%2C20.4-10.7%2C31-6.2c12.5%2C5.4%2C19.6%2C18.8%2C17%2C32.2C60%2C54%2C48.3%2C63.8%2C34.7%2C64z%22/%3E%3C/svg%3E"
    return await _call(domain, token, "imconnector.register", {
        "ID": connector_id,
        "NAME": connector_name,
        "ICON": {"DATA_IMAGE": svg, "COLOR": "#0088cc", "SIZE": "100%", "POSITION": "center"},
        "PLACEMENT_HANDLER": placement_handler,
        "CHAT_GROUP": True,
        "NEED_SIGNATURE": False,
    })


async def bind_event(domain: str, token_enc: str, event: str, handler: str):
    token = _get_token(token_enc)
    return await _call(domain, token, "event.bind", {"event": event, "handler": handler})


async def activate_connector(domain: str, token_enc: str, connector_id: str, line: int, active: int = 1):
    token = _get_token(token_enc)
    return await _call(domain, token, "imconnector.activate", {
        "CONNECTOR": connector_id, "LINE": line, "ACTIVE": active,
    })


async def set_connector_data(domain: str, token_enc: str, connector_id: str, line: int, data: dict):
    token = _get_token(token_enc)
    normalized = {
        "ID": data.get("ID") or data.get("id") or f"{connector_id}_line_{line}",
        "URL": data.get("URL") or data.get("url") or "",
        "URL_IM": data.get("URL_IM") or data.get("url_im") or data.get("URL") or data.get("url") or "",
        "NAME": data.get("NAME") or data.get("name") or connector_id,
    }
    return await _call(domain, token, "imconnector.connector.data.set", {
        "CONNECTOR": connector_id, "LINE": line, "DATA": normalized,
    })


async def send_messages(domain: str, token_enc: str, connector_id: str, line: int, messages: list):
    token = _get_token(token_enc)
    return await _call(domain, token, "imconnector.send.messages", {
        "CONNECTOR": connector_id, "LINE": line, "MESSAGES": messages,
    })


async def send_delivery(domain: str, token_enc: str, connector_id: str, line: int, im: dict, message_ids: list, chat_id: str):
    token = _get_token(token_enc)
    return await _call(domain, token, "imconnector.send.status.delivery", {
        "CONNECTOR": connector_id, "LINE": line,
        "MESSAGES": [{"im": im, "message": {"id": message_ids}, "chat": {"id": chat_id}}],
    })


async def get_open_lines(domain: str, token_enc: str):
    token = _get_token(token_enc)
    return await _call(domain, token, "imopenlines.config.list.get", {})


async def _get_app_disk_folder_id(domain: str, token: str) -> int:
    now = time.monotonic()
    cache_ttl = max(60, int(settings.cache_ttl_seconds or 3600))
    cache_limit = max(10, int(settings.cache_max_size or 1000))
    cache_key = (domain or "").strip().lower()
    cached = _app_disk_folder_ids.get(cache_key)
    if cached and now - cached[1] <= cache_ttl:
        return cached[0]
    if len(_app_disk_folder_ids) > cache_limit:
        stale = [
            key for key, (_, created_at) in _app_disk_folder_ids.items()
            if now - created_at > cache_ttl
        ]
        for key in stale:
            _app_disk_folder_ids.pop(key, None)
        if len(_app_disk_folder_ids) > cache_limit:
            for key, _ in sorted(_app_disk_folder_ids.items(), key=lambda item: item[1][1])[: len(_app_disk_folder_ids) - cache_limit]:
                _app_disk_folder_ids.pop(key, None)
    result = await _call(domain, token, "disk.storage.getforapp", {})
    folder_id = int(result.get("ROOT_OBJECT_ID") or 0) if result else 0
    if folder_id:
        _app_disk_folder_ids[cache_key] = (folder_id, now)
    return folder_id


async def upload_file_to_disk(domain: str, token_enc: str, file_bytes: bytes, file_name: str):
    """Upload file to Bitrix24 app disk storage. Returns disk file_id or None."""
    token = _get_token(token_enc)
    folder_id = await _get_app_disk_folder_id(domain, token)
    if not folder_id:
        logger.error("Bitrix24 disk: could not get app folder ID")
        return None
    b64 = base64.b64encode(file_bytes).decode()
    result = await _call(domain, token, "disk.folder.uploadfile", {
        "id": folder_id,
        "data": {"NAME": file_name},
        "fileContent": [file_name, b64],
    }, timeout=60)
    if not result:
        return None
    return result.get("ID")


async def commit_file_to_chat(domain: str, token_enc: str, bitrix_chat_id: str, disk_file_id: int):
    """Commit a Bitrix24 disk file to an IM Open Lines chat. Returns message_id or None."""
    token = _get_token(token_enc)
    result = await _call(domain, token, "im.disk.file.commit", {
        "DIALOG_ID": f"chat{bitrix_chat_id}",
        "UPLOAD_ID": disk_file_id,
    })
    if result and isinstance(result, dict):
        return result.get("MESSAGE_ID")
    return None
