#!/usr/bin/env python3
import io
import fcntl
import hashlib
import json
import os
import re
import signal
import time
import threading
import uuid
from collections import deque
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from zipfile import BadZipFile

import requests
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.utils import column_index_from_string

# ===== Bitrix settings =====
REST_BASE = os.getenv("BITRIX_REST_BASE", "")
BOT_ID = int(os.getenv("BITRIX_BOT_ID", "0") or "0")
BOT_TOKEN = os.getenv("BITRIX_BOT_TOKEN", "")


def env_int(name, default):
    try:
        return int(os.getenv(name, str(default)) or default)
    except Exception:
        return int(default)

# Empty set means any Bitrix user who can write to the bot is allowed.
ALLOWED_USER_IDS = set()

# ===== FF settings =====
URL = "https://ff-platform.ru/lk/1/client_api/v1/sklad_dispatch_by_cdek_number/create"
FF_TOKEN = os.getenv("FF_TOKEN", "")
SKLAD_ID = 2

# ===== Script behavior =====
SENT_LOG = os.getenv("SENT_LOG", "sent_nakls.txt")
PROCESSED_FILES_LOG = os.getenv("PROCESSED_FILES_LOG", "processed_files.txt")
PROCESSED_HASHES_LOG = os.getenv("PROCESSED_HASHES_LOG", "processed_hashes.txt")
OFFSET_FILE = os.getenv("OFFSET_FILE", "event_offset.txt")
BATCH_RUNS_LOG = os.getenv("BATCH_RUNS_LOG", "batch_runs.jsonl")
BATCH_ITEMS_LOG = os.getenv("BATCH_ITEMS_LOG", "batch_items.jsonl")
FAILED_NAKLS_LOG = os.getenv("FAILED_NAKLS_LOG", "failed_nakls.jsonl")
KNOWN_DIALOGS_LOG = os.getenv("KNOWN_DIALOGS_LOG", "known_dialogs.txt")
KNOWN_CHAT_IDS_LOG = os.getenv("KNOWN_CHAT_IDS_LOG", "known_chat_ids.txt")
INCOMING_EVENTS_LOG = os.getenv("INCOMING_EVENTS_LOG", "incoming_events.jsonl")
CHAT_INDEX_LOG = os.getenv("CHAT_INDEX_LOG", "chat_index.jsonl")
FILE_DOWNLOAD_FAILURES_LOG = os.getenv("FILE_DOWNLOAD_FAILURES_LOG", "file_download_failures.jsonl")
FILE_JOBS_LOG = os.getenv("FILE_JOBS_LOG", "file_jobs.jsonl")
FAILED_FILES_LOG = os.getenv("FAILED_FILES_LOG", "failed_files.jsonl")
USER_EVENTS_MARKER_FILE = os.getenv("USER_EVENTS_MARKER_FILE", "user_events_enabled.marker")

COLUMN = "A"
SKIP_HEADER = False
SHEET_NAME = None
TIMEOUT = 30
DELAY = 5.0
FORCE = False
POLL_SECONDS = 3
EVENT_FETCH_LIMIT = max(1, min(env_int("EVENT_FETCH_LIMIT", 100), 1000))
FILE_PROCESS_TIMEOUT = 900   # максимум секунд на обработку одного файла
DEBOUNCE_SECONDS = 30        # ждём тишины перед обработкой файла
MAX_SEND_ATTEMPTS = 3
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
RETRY_BASE_DELAY = 10.0
DRY_RUN = os.getenv("FF_DRY_RUN", "").lower() in {"1", "true", "yes", "y"}
HISTORY_SCAN_SECONDS = 60
HISTORY_SCAN_LIMIT = 50
OFFSET_RESET_PROBE_SECONDS = 60
BOT_CHAT_SCAN_SECONDS = 120
DISK_RECOVERY_MAX_AGE_SECONDS = env_int("DISK_RECOVERY_MAX_AGE_SECONDS", 3 * 24 * 3600)
FILE_JOB_RETRY_SECONDS = env_int("FILE_JOB_RETRY_SECONDS", 10 * 60)
MAX_UPLOAD_BYTES = env_int("MAX_UPLOAD_BYTES", 100 * 1024 * 1024)
NO_FILE_REPLY_TTL_SECONDS = env_int("NO_FILE_REPLY_TTL_SECONDS", 10 * 60)
MAX_STATUS_MESSAGE_CHARS = env_int("MAX_STATUS_MESSAGE_CHARS", 3500)
WITH_USER_EVENTS = os.getenv("BITRIX_WITH_USER_EVENTS", "1").lower() not in {"0", "false", "no", "n", "off"}


# ─── debounce-очередь ───────────────────────────────────────────────────────
# { file_id_str: {"dialog_id": ..., "timer": Timer} }
_debounce: dict = {}
_debounce_lock = threading.Lock()
_active: set = set()  # file_id которые прямо сейчас обрабатываются
_active_lock = threading.Lock()
_audit_lock = threading.Lock()
_shutdown_requested = threading.Event()
_last_history_scan_at = 0.0
_last_zero_offset_probe_at = 0.0
_last_bot_chat_scan_at = 0.0
_chat_index_cache = {}
_history_scan_suppressed_until = {}
_no_file_reply_until = {}
_with_user_events_enabled = WITH_USER_EVENTS
_user_events_subscribed = False

# ───────────────────────── helpers ──────────────────────────────────────────

def normalize_nakl(value):
    if value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        s = format(value, "f").rstrip("0").rstrip(".")
        return s if s.isdigit() else None
    s = str(value).strip().replace(" ", "")
    if not s:
        return None
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s if s.isdigit() else None


def read_nakls_from_excel(path, sheet_name=None, column="A", skip_header=False):
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb[sheet_name] if sheet_name else wb.active
    col_idx = column_index_from_string(column.upper())
    rows = ws.iter_rows(min_col=col_idx, max_col=col_idx, values_only=True)
    if skip_header:
        next(rows, None)
    result, seen = [], set()
    for (value,) in rows:
        nakl = normalize_nakl(value)
        if not nakl:
            continue
        if nakl not in seen:
            result.append(nakl)
            seen.add(nakl)
    return result


def load_sent_log(path):
    p = Path(path)
    if not p.exists():
        return set()
    return {line.strip() for line in p.read_text(encoding="utf-8").splitlines() if line.strip()}


def append_sent_log(path, nakl):
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{nakl}\n")


def load_simple_log(path):
    p = Path(path)
    if not p.exists():
        return set()
    return {line.strip() for line in p.read_text(encoding="utf-8").splitlines() if line.strip()}


def append_simple_log(path, value):
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{value}\n")


def safe_int(value):
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    return [value]


def append_unique_log(path, value):
    if value is None or value == "":
        return
    value = str(value)
    if value not in load_simple_log(path):
        append_simple_log(path, value)


def iter_jsonl(path, max_lines=None):
    p = Path(path)
    if not p.exists():
        return
    if max_lines is None:
        lines = p.open("r", encoding="utf-8", errors="replace")
    else:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            lines = deque(f, maxlen=max_lines)
    for line in lines:
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue
    if max_lines is None:
        lines.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def redact_secret_text(value):
    text = "" if value is None else str(value)
    secrets = [REST_BASE, BOT_TOKEN, FF_TOKEN]
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    text = re.sub(r"https://([^/]+)/rest/\d+/[^/]+", r"https://\1/rest/[redacted]", text)
    text = re.sub(r"/rest/(\d+)/[^/\s]+", r"/rest/\1/[redacted]", text)
    return text


def short_text(value, limit=1000):
    if value is None:
        return ""
    text = redact_secret_text(value).replace("\r", " ").replace("\n", " ")
    return text[:limit]


def append_jsonl(path, record):
    record = {"ts": now_iso(), **record}
    line = json.dumps(record, ensure_ascii=False, sort_keys=True)
    with _audit_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def append_batch_run(record):
    append_jsonl(BATCH_RUNS_LOG, record)


def append_batch_item(record):
    append_jsonl(BATCH_ITEMS_LOG, record)


def append_failed_nakl(record):
    append_jsonl(FAILED_NAKLS_LOG, record)


def load_offset():
    p = Path(OFFSET_FILE)
    if not p.exists():
        return 0
    try:
        return int(p.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def save_offset(offset):
    Path(OFFSET_FILE).write_text(str(offset), encoding="utf-8")


def remember_dialog(dialog_id):
    if dialog_id is None or dialog_id == "":
        return
    append_unique_log(KNOWN_DIALOGS_LOG, str(dialog_id))


def remember_chat_id(chat_id):
    chat_id = safe_int(chat_id)
    if chat_id:
        append_unique_log(KNOWN_CHAT_IDS_LOG, str(chat_id))


def _remember_dialog_from_record(dialogs, record):
    for key in ("dialog_id", "dialogId", "download_dialog", "request_dialog"):
        value = record.get(key)
        if value not in {None, ""}:
            dialogs.add(str(value))
    for key in ("user_id", "userId", "author_id", "authorId", "created_by", "CREATED_BY"):
        value = safe_int(record.get(key))
        if value and value != BOT_ID:
            dialogs.add(str(value))
    raw = record.get("raw")
    if isinstance(raw, dict):
        dialog_id = extract_dialog_id(raw)
        user_id = extract_author_id(raw)
        if dialog_id:
            dialogs.add(str(dialog_id))
        if user_id and user_id != BOT_ID:
            dialogs.add(str(user_id))


def load_known_dialogs():
    dialogs = load_simple_log(KNOWN_DIALOGS_LOG)
    for path in (BATCH_RUNS_LOG, INCOMING_EVENTS_LOG, CHAT_INDEX_LOG, FILE_DOWNLOAD_FAILURES_LOG):
        for record in iter_jsonl(path, max_lines=5000) or []:
            if isinstance(record, dict):
                _remember_dialog_from_record(dialogs, record)
                chat = record.get("chat")
                if isinstance(chat, dict):
                    dialog_id = chat.get("dialogId") or chat.get("dialog_id")
                    if dialog_id:
                        dialogs.add(str(dialog_id))
    dialogs.discard("")
    dialogs.discard(str(BOT_ID))
    return dialogs


def load_known_chat_ids():
    chat_ids = set()
    for value in load_simple_log(KNOWN_CHAT_IDS_LOG):
        chat_id = safe_int(value)
        if chat_id:
            chat_ids.add(chat_id)
    for record in iter_jsonl(CHAT_INDEX_LOG, max_lines=5000) or []:
        if not isinstance(record, dict):
            continue
        chat_id = safe_int(record.get("chat_id"))
        if chat_id:
            chat_ids.add(chat_id)
        chat = record.get("chat")
        if isinstance(chat, dict):
            chat_id = safe_int(chat.get("id"))
            if chat_id:
                chat_ids.add(chat_id)
    return chat_ids


def make_job_key(dialog_id, file_id):
    return f"{str(dialog_id)}:{str(file_id)}"


def compact_job_source(source):
    source = source or {}
    if not isinstance(source, dict):
        return {}
    out = {}
    for key in (
        "source", "dialog_id", "download_dialog", "user_id", "author_id", "chat_id",
        "file_name", "download_url", "DOWNLOAD_URL",
    ):
        if source.get(key) not in {None, ""}:
            out[key] = source.get(key)
    raw_file = source.get("raw_file")
    if isinstance(raw_file, dict):
        out["raw_file"] = {
            k: raw_file.get(k)
            for k in ("id", "ID", "fileId", "FILE_ID", "NAME", "name", "DOWNLOAD_URL", "downloadUrl")
            if raw_file.get(k) not in {None, ""}
        }
    return out


def append_file_job(status, dialog_id, file_id, source=None, reason=None):
    append_jsonl(FILE_JOBS_LOG, {
        "status": status,
        "job_key": make_job_key(dialog_id, file_id),
        "dialog_id": str(dialog_id) if dialog_id is not None else None,
        "file_id": int(file_id) if safe_int(file_id) else file_id,
        "source": compact_job_source(source),
        "reason": short_text(reason, 1000) if reason else None,
    })


def append_failed_file(dialog_id, file_id, reason, source=None, terminal=True):
    append_jsonl(FAILED_FILES_LOG, {
        "job_key": make_job_key(dialog_id, file_id),
        "dialog_id": str(dialog_id) if dialog_id is not None else None,
        "file_id": int(file_id) if safe_int(file_id) else file_id,
        "terminal": terminal,
        "reason": short_text(reason, 1000),
        "source": compact_job_source(source),
    })


def parse_iso_ts(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def load_file_job_states():
    states = {}
    for record in iter_jsonl(FILE_JOBS_LOG, max_lines=10000) or []:
        if not isinstance(record, dict):
            continue
        key = record.get("job_key")
        if not key:
            dialog_id = record.get("dialog_id")
            file_id = record.get("file_id")
            if dialog_id is None or file_id is None:
                continue
            key = make_job_key(dialog_id, file_id)
        states[str(key)] = record
    return states


def should_recover_job(record):
    status = record.get("status")
    if status in {"queued", "started"}:
        return True
    if status != "retryable_failed":
        return False
    ts = parse_iso_ts(record.get("ts"))
    if not ts:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(ts.tzinfo) - ts).total_seconds() >= FILE_JOB_RETRY_SECONDS


def is_job_terminal(dialog_id, file_id):
    state = load_file_job_states().get(make_job_key(dialog_id, file_id))
    if not state:
        return False
    return state.get("status") in {"completed", "terminal_failed"}


def is_file_queued_or_active(file_id, dialog_id=None):
    key = make_job_key(dialog_id, file_id) if dialog_id is not None else None
    file_id_str = str(file_id)
    if file_id_str in load_simple_log(PROCESSED_FILES_LOG):
        return True
    with _active_lock:
        if (key and key in _active) or file_id_str in _active:
            return True
    with _debounce_lock:
        if (key and key in _debounce) or file_id_str in _debounce:
            return True
    return False


def _history_files(result):
    files = result.get("files", []) if isinstance(result, dict) else []
    if isinstance(files, dict):
        return list(files.values())
    if isinstance(files, list):
        return files
    return []


def _history_download_dialog(request_dialog_id, file_info):
    request_dialog = str(request_dialog_id)
    if request_dialog.startswith("chat"):
        return request_dialog
    author_id = file_info.get("authorId") or file_info.get("author_id") or file_info.get("AUTHOR_ID")
    if request_dialog == str(BOT_ID) and author_id:
        return str(author_id)
    return request_dialog


def _file_info_id(file_info):
    if not isinstance(file_info, dict):
        return None
    for key in ("id", "ID", "fileId", "FILE_ID", "objectId", "OBJECT_ID"):
        file_id = safe_int(file_info.get(key))
        if file_id:
            return file_id
    return None


def _file_info_name(file_info):
    if not isinstance(file_info, dict):
        return ""
    return file_info.get("name") or file_info.get("NAME") or file_info.get("title") or file_info.get("TITLE") or ""


def _file_info_ext(file_info):
    if not isinstance(file_info, dict):
        return ""
    name = _file_info_name(file_info)
    return str(file_info.get("extension") or file_info.get("EXTENSION") or Path(name).suffix.lstrip(".")).lower()


def _parse_bitrix_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _file_info_created_at(file_info):
    if not isinstance(file_info, dict):
        return None
    for key in ("CREATE_TIME", "createTime", "create_time", "date", "DATE_CREATE"):
        dt = _parse_bitrix_time(file_info.get(key))
        if dt:
            return dt
    return None


def _known_file_id_from_audit(file_id):
    file_id_str = str(file_id)
    for path in (INCOMING_EVENTS_LOG, FILE_DOWNLOAD_FAILURES_LOG, BATCH_RUNS_LOG):
        for record in iter_jsonl(path, max_lines=5000) or []:
            if not isinstance(record, dict):
                continue
            if str(record.get("file_id")) == file_id_str:
                return True
            for value in as_list(record.get("file_ids")):
                if str(value) == file_id_str:
                    return True
    return False


def _is_recent_recovery_file(file_info, file_id):
    if _known_file_id_from_audit(file_id):
        return True
    created_at = _file_info_created_at(file_info)
    if not created_at:
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    now = datetime.now(created_at.tzinfo or timezone.utc)
    return (now - created_at).total_seconds() <= DISK_RECOVERY_MAX_AGE_SECONDS


def _is_supported_excel(file_info):
    ext = _file_info_ext(file_info)
    name = _file_info_name(file_info).lower()
    return ext in {"xlsx", "xlsm"} or name.endswith((".xlsx", ".xlsm"))


def _queue_file_from_info(dialog_id, file_info, source_name):
    processed = load_simple_log(PROCESSED_FILES_LOG)
    file_id = _file_info_id(file_info)
    if not file_id:
        return False
    file_id_str = str(file_id)
    if file_id_str in processed or is_job_terminal(dialog_id, file_id) or is_file_queued_or_active(file_id, dialog_id):
        return False
    if source_name == "disk" and not _is_recent_recovery_file(file_info, file_id):
        return False
    if not _is_supported_excel(file_info):
        reason = f"unsupported_file_type: name={_file_info_name(file_info)} ext={_file_info_ext(file_info)}"
        append_failed_file(dialog_id, file_id, reason, source={"source": source_name, "raw_file": file_info}, terminal=True)
        append_file_job("terminal_failed", dialog_id, file_id, source={"source": source_name, "raw_file": file_info}, reason=reason)
        print(
            f"[{source_name}] skip non-excel file_id={file_id} dialog={dialog_id} "
            f"name={short_text(_file_info_name(file_info), 120)} ext={_file_info_ext(file_info)}",
            flush=True,
        )
        return False

    download_dialog = _history_download_dialog(dialog_id, file_info)
    source = {
        "source": source_name,
        "dialog_id": str(download_dialog),
        "download_dialog": str(download_dialog),
        "file_name": _file_info_name(file_info),
        "download_url": file_info.get("DOWNLOAD_URL") or file_info.get("downloadUrl") or file_info.get("download_url"),
        "raw_file": file_info,
    }
    print(
        f"[{source_name}] found unprocessed file_id={file_id} dialog={download_dialog} "
        f"name={short_text(_file_info_name(file_info), 120)} ext={_file_info_ext(file_info)}",
        flush=True,
    )
    remember_dialog(download_dialog)
    schedule_file(download_dialog, file_id, source=source)
    return True


def recover_visible_history_files():
    scan_targets = {str(d) for d in load_known_dialogs()}
    scan_targets.update(f"chat{chat_id}" for chat_id in load_known_chat_ids())
    for dialog_id in sorted(scan_targets):
        suppressed_until = _history_scan_suppressed_until.get(dialog_id, 0)
        if suppressed_until and time.monotonic() < suppressed_until:
            continue
        try:
            result = b24("im.dialog.messages.get", {"DIALOG_ID": str(dialog_id), "LIMIT": HISTORY_SCAN_LIMIT})
        except Exception as e:
            if "403" in str(e) or "Forbidden" in str(e):
                _history_scan_suppressed_until[dialog_id] = time.monotonic() + 3600
            print(f"[history] scan failed dialog={dialog_id}: {short_text(e, 500)}", flush=True)
            continue

        for file_info in _history_files(result):
            _queue_file_from_info(dialog_id, file_info, "history")


def recover_bot_chat_disk_files():
    for dialog_id in sorted(load_known_dialogs()):
        meta = index_bot_chat(dialog_id, source="recovery")
        if not meta:
            continue
        disk_folder_id = safe_int(meta.get("disk_folder_id"))
        if not disk_folder_id:
            continue
        start = 0
        while True:
            try:
                data = b24_full("disk.folder.getchildren", {"id": disk_folder_id, "start": start})
            except Exception as e:
                print(f"[disk] scan failed dialog={dialog_id} folder={disk_folder_id} start={start}: {short_text(e, 500)}", flush=True)
                break
            items = data.get("result", [])
            if isinstance(items, dict):
                items = list(items.values())
            for file_info in items or []:
                if isinstance(file_info, dict) and str(file_info.get("TYPE") or file_info.get("type") or "").lower() != "file":
                    continue
                _queue_file_from_info(meta.get("dialog_id") or dialog_id, file_info, "disk")
            next_start = data.get("next")
            if next_start in {None, "", False}:
                break
            try:
                start = int(next_start)
            except Exception:
                break


def recover_recent_history_files():
    recover_bot_chat_disk_files()
    recover_visible_history_files()


def recover_pending_file_jobs():
    recovered = 0
    for record in load_file_job_states().values():
        if not should_recover_job(record):
            continue
        dialog_id = record.get("dialog_id")
        file_id = record.get("file_id")
        if dialog_id is None or file_id is None:
            continue
        if is_file_queued_or_active(file_id, dialog_id):
            continue
        source = record.get("source") if isinstance(record.get("source"), dict) else {}
        print(f"[jobs] recover pending job={make_job_key(dialog_id, file_id)} status={record.get('status')}", flush=True)
        schedule_file(dialog_id, file_id, source={**source, "source": source.get("source") or "job_recovery"}, persist=False)
        recovered += 1
    return recovered


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def acquire_file_lock(name):
    lock_path = f"/tmp/ff-bot-{name}.lock"
    f = open(lock_path, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except BlockingIOError:
        f.close()
        return None


def acquire_blocking_lock(name):
    lock_path = f"/tmp/ff-bot-{name}.lock"
    f = open(lock_path, "w")
    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    return f


def request_shutdown(signum, _frame):
    print(f"Shutdown requested by signal {signum}. Waiting active file processing before exit.", flush=True)
    _shutdown_requested.set()


def active_count():
    with _active_lock:
        return len(_active)


# ───────────────────────── Bitrix API ───────────────────────────────────────

def b24_full(method, payload):
    r = requests.post(f"{REST_BASE}/{method}", json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"{data.get('error')}: {data.get('error_description')}")
    return data


def b24(method, payload):
    data = b24_full(method, payload)
    return data.get("result", {})


def ensure_user_event_subscription(force=False):
    """Enable ONIMV2* delivery for the current webhook user when Bitrix allows it."""
    global _user_events_subscribed, _with_user_events_enabled
    if not _with_user_events_enabled:
        return False
    if _user_events_subscribed and not force:
        return False

    first_enable = not Path(USER_EVENTS_MARKER_FILE).exists()
    try:
        b24("im.v2.Event.subscribe", {})
        _user_events_subscribed = True
        if first_enable:
            Path(USER_EVENTS_MARKER_FILE).write_text(now_iso(), encoding="utf-8")
        print("[events] user-events subscription active; withUserEvents enabled", flush=True)
        return first_enable
    except Exception as e:
        _with_user_events_enabled = False
        print(f"[events] user-events disabled: subscribe failed: {short_text(e, 500)}", flush=True)
        return False


def _is_user_events_error(exc):
    text = str(exc)
    return any(code in text for code in ("SCOPE_ERROR", "AUTH_ERROR", "USER_NOT_SUBSCRIBED"))


def index_bot_chat(dialog_id, source=""):
    if dialog_id is None or dialog_id == "":
        return None
    dialog_id = str(dialog_id)
    try:
        result = b24("imbot.v2.Chat.get", {
            "botId": BOT_ID,
            "botToken": BOT_TOKEN,
            "dialogId": dialog_id,
        })
    except Exception as e:
        append_jsonl(CHAT_INDEX_LOG, {
            "event": "chat_index_failed",
            "source": source,
            "dialog_id": dialog_id,
            "error": short_text(e, 500),
        })
        return None

    chat = result.get("chat") if isinstance(result, dict) else {}
    users = result.get("users") if isinstance(result, dict) else []
    if not isinstance(chat, dict):
        chat = {}
    actual_dialog = chat.get("dialogId") or chat.get("dialog_id") or dialog_id
    chat_id = safe_int(chat.get("id"))
    disk_folder_id = safe_int(chat.get("diskFolderId") or chat.get("disk_folder_id"))
    if actual_dialog:
        remember_dialog(actual_dialog)
    if chat_id:
        remember_chat_id(chat_id)

    record = {
        "event": "chat_indexed",
        "source": source,
        "dialog_id": str(actual_dialog) if actual_dialog else dialog_id,
        "request_dialog": dialog_id,
        "chat_id": chat_id,
        "disk_folder_id": disk_folder_id,
        "last_message_id": chat.get("lastMessageId") or chat.get("lastId"),
        "message_count": chat.get("messageCount"),
        "chat": chat,
        "users": users,
    }
    cache_key = str(actual_dialog) if actual_dialog else dialog_id
    cache_value = (
        record["chat_id"],
        record["disk_folder_id"],
        record["last_message_id"],
        record["message_count"],
    )
    if _chat_index_cache.get(cache_key) != cache_value:
        append_jsonl(CHAT_INDEX_LOG, record)
        _chat_index_cache[cache_key] = cache_value
    return record


def send_bot_message(dialog_id, text):
    b24("imbot.v2.Chat.Message.send", {
        "botId": BOT_ID,
        "botToken": BOT_TOKEN,
        "dialogId": str(dialog_id),
        "fields": {"message": text[:4000]},
    })


def safe_send_bot_message(dialog_id, text):
    try:
        send_bot_message(dialog_id, text)
        return True
    except Exception as e:
        print(f"Не удалось отправить сообщение в Bitrix dialog_id={dialog_id}: {short_text(e, 500)}; text={short_text(text, 300)}", flush=True)
        return False


def safe_send_bot_lines(dialog_id, lines):
    chunk = ""
    sent_any = False
    for line in lines:
        line = str(line)
        piece = line if not chunk else "\n" + line
        if len(chunk) + len(piece) > MAX_STATUS_MESSAGE_CHARS:
            if chunk:
                sent_any = safe_send_bot_message(dialog_id, chunk) or sent_any
            chunk = line
        else:
            chunk += piece
    if chunk:
        sent_any = safe_send_bot_message(dialog_id, chunk) or sent_any
    return sent_any


def get_events(offset=0):
    global _with_user_events_enabled
    payload = {
        "botId": BOT_ID,
        "botToken": BOT_TOKEN,
        "limit": EVENT_FETCH_LIMIT,
    }
    if _with_user_events_enabled:
        payload["withUserEvents"] = True
    # Do not send a stale positive offset after switching the bot to fetch mode.
    # Bitrix treats offset as acknowledgement for all lower event ids.
    if offset and offset > 0:
        payload["offset"] = offset
    try:
        result = b24("imbot.v2.Event.get", payload)
    except Exception as e:
        if not payload.get("withUserEvents") or not _is_user_events_error(e):
            raise
        _with_user_events_enabled = False
        payload.pop("withUserEvents", None)
        print(f"[events] withUserEvents disabled after Bitrix error: {short_text(e, 500)}", flush=True)
        result = b24("imbot.v2.Event.get", payload)
    events = result.get("events", [])
    next_offset = result.get("nextOffset", offset)
    has_more = result.get("hasMore", False)
    return events, next_offset, has_more


def _get_dict(value):
    return value if isinstance(value, dict) else {}


def _event_data(ev):
    if not isinstance(ev, dict):
        return {}, {}, {}, {}
    d = _get_dict(ev.get("data") or ev.get("DATA"))
    m = _get_dict(d.get("message") or d.get("MESSAGE") or ev.get("message") or ev.get("MESSAGE"))
    chat = _get_dict(d.get("chat") or d.get("CHAT") or m.get("chat") or m.get("CHAT"))
    params = _get_dict(m.get("params") or m.get("PARAMS") or d.get("params") or d.get("PARAMS"))
    return d, m, chat, params


def extract_chat_id(ev):
    d, m, chat, _params = _event_data(ev)
    for value in (
        chat.get("id"), chat.get("ID"), chat.get("chatId"), chat.get("CHAT_ID"),
        d.get("chatId"), d.get("chat_id"), d.get("CHAT_ID"),
        m.get("chatId"), m.get("chat_id"), m.get("CHAT_ID"),
    ):
        chat_id = safe_int(value)
        if chat_id:
            return chat_id
    return None


def extract_author_id(ev):
    d, m, _chat, params = _event_data(ev)
    user = _get_dict(d.get("user") or d.get("USER") or ev.get("user") or ev.get("USER"))
    for value in (
        user.get("id"), user.get("ID"),
        d.get("userId"), d.get("user_id"), d.get("USER_ID"),
        d.get("authorId"), d.get("author_id"), d.get("AUTHOR_ID"),
        m.get("authorId"), m.get("author_id"), m.get("AUTHOR_ID"),
        m.get("senderId"), m.get("sender_id"), m.get("SENDER_ID"),
        params.get("USER_ID"), params.get("AUTHOR_ID"),
    ):
        uid = safe_int(value)
        if uid:
            return uid
    return None


def _non_bot_user_from_chat(chat):
    users = chat.get("users") or chat.get("USERS")
    for user in as_list(users):
        if not isinstance(user, dict):
            continue
        uid = safe_int(user.get("id") or user.get("ID"))
        if uid and uid != BOT_ID:
            return uid
    return None


def extract_dialog_id(ev):
    d, m, chat, params = _event_data(ev)
    for value in (
        chat.get("dialogId"), chat.get("dialog_id"), chat.get("DIALOG_ID"),
        d.get("dialogId"), d.get("dialog_id"), d.get("DIALOG_ID"),
        m.get("dialogId"), m.get("dialog_id"), m.get("DIALOG_ID"),
        params.get("DIALOG_ID"), params.get("dialogId"), params.get("dialog_id"),
    ):
        if value not in {None, ""}:
            return str(value)

    user_id = extract_author_id(ev)
    chat_type = str(
        chat.get("type") or chat.get("TYPE") or chat.get("messageType") or chat.get("MESSAGE_TYPE") or ""
    ).lower()
    if user_id and user_id != BOT_ID and chat_type in {"private", "p"}:
        return str(user_id)

    non_bot_user = _non_bot_user_from_chat(chat)
    if non_bot_user:
        return str(non_bot_user)

    chat_id = extract_chat_id(ev)
    if chat_id:
        if user_id and user_id != BOT_ID and not chat_type:
            return str(user_id)
        if chat_type in {"private", "p"} and user_id and user_id != BOT_ID:
            return str(user_id)
        return f"chat{chat_id}"
    if user_id and user_id != BOT_ID:
        return str(user_id)
    return None


def _coerce_file_ids(value):
    ids = []
    if value is None or value is False:
        return ids
    if isinstance(value, int) and not isinstance(value, bool):
        return [value]
    if isinstance(value, str):
        value = value.strip()
        return [int(value)] if value.isdigit() else []
    if isinstance(value, dict):
        for key in ("id", "ID", "fileId", "FILE_ID", "file_id", "objectId", "OBJECT_ID"):
            ids.extend(_coerce_file_ids(value.get(key)))
        return ids
    if isinstance(value, (list, tuple, set)):
        for item in value:
            ids.extend(_coerce_file_ids(item))
        return ids
    return ids


def _collect_file_ids(value, in_file_context=False):
    ids = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_l = str(key).lower()
            file_key = (
                key_l in {
                    "file", "files", "file_id", "file_ids", "fileid", "fileids",
                    "disk_file_id", "diskfileid", "uploadfileid", "objectid",
                }
                or ("file" in key_l and "id" in key_l)
            )
            child_context = in_file_context or key_l in {"file", "files", "filedata", "file_data"} or file_key
            if file_key:
                ids.extend(_coerce_file_ids(child))
            elif child_context and key_l in {"id", "ID".lower(), "object_id", "objectid"}:
                ids.extend(_coerce_file_ids(child))
            ids.extend(_collect_file_ids(child, child_context))
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            ids.extend(_collect_file_ids(child, in_file_context))
    return ids


def extract_file_ids(ev):
    d, m, _chat, p = _event_data(ev)

    candidates = [
        p.get("FILE_ID"), p.get("FILE_IDS"), p.get("file_id"), p.get("file_ids"),
        p.get("FILES"), p.get("files"), p.get("FILE"), p.get("file"),
        m.get("file") if isinstance(m, dict) else None,
        m.get("files") if isinstance(m, dict) else None,
        d.get("file") if isinstance(d, dict) else None,
        d.get("files") if isinstance(d, dict) else None,
        d.get("fileId") if isinstance(d, dict) else None,
        d.get("FILE_ID") if isinstance(d, dict) else None,
    ]
    out = []
    seen = set()
    for candidate in candidates:
        for file_id in _coerce_file_ids(candidate):
            if file_id not in seen:
                seen.add(file_id)
                out.append(file_id)
    for file_id in _collect_file_ids(ev):
        if file_id not in seen:
            seen.add(file_id)
            out.append(file_id)
    return out


def extract_file_id(ev):
    ids = extract_file_ids(ev)
    return ids[0] if ids else None


def event_brief(ev):
    d, m, _chat, p = _event_data(ev)
    text = (m.get("text") or "") if isinstance(m, dict) else ""
    return (
        f"type={ev.get('type') or ev.get('event') or ev.get('EVENT')} "
        f"user={extract_author_id(ev)} dialog={extract_dialog_id(ev)} chat={extract_chat_id(ev)} "
        f"files={extract_file_ids(ev)} params={list(p.keys())[:12]} text={short_text(text, 120)}"
    )


def is_message_event(ev):
    event_type = str(ev.get("type") or ev.get("event") or ev.get("EVENT") or "").upper() if isinstance(ev, dict) else ""
    return event_type in {
        "ONIMBOTV2MESSAGEADD",
        "ONIMBOTV2MESSAGEUPDATE",
        "ONIMBOTMESSAGEADD",
        "ONIMBOTMESSAGEUPDATE",
        "ONIMV2MESSAGEADD",
        "ONIMV2MESSAGEUPDATE",
        "ONIMMESSAGEADD",
        "ONIMMESSAGEUPDATE",
        "MESSAGE_ADD",
        "MESSAGE_UPDATE",
        "MESSAGEADD",
        "MESSAGEUPDATE",
    }


def get_download_url_by_file_id(dialog_id, file_id):
    result = b24("imbot.v2.File.download", {
        "botId": BOT_ID,
        "botToken": BOT_TOKEN,
        "dialogId": str(dialog_id),
        "fileId": int(file_id),
    })
    if isinstance(result, str) and result.startswith("http"):
        return result
    if isinstance(result, dict):
        for key in ("url", "URL", "downloadUrl", "DOWNLOAD_URL"):
            v = result.get(key)
            if isinstance(v, str) and v.startswith("http"):
                return v
    return None


def disk_file_download_url(file_id):
    result = b24("disk.file.get", {"id": int(file_id)})
    if isinstance(result, str) and result.startswith("http"):
        return result
    if isinstance(result, dict):
        for key in ("DOWNLOAD_URL", "downloadUrl", "download_url", "url", "URL"):
            value = result.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
    return None


def _add_candidate(candidates, value):
    if value is None or value == "":
        return
    value = str(value)
    if value not in candidates:
        candidates.append(value)


def dialog_download_candidates(dialog_id, source=None):
    candidates = []
    source = source or {}
    raw = source.get("raw") if isinstance(source, dict) else None
    _add_candidate(candidates, source.get("download_dialog") if isinstance(source, dict) else None)
    _add_candidate(candidates, dialog_id)
    if isinstance(source, dict):
        _add_candidate(candidates, source.get("dialog_id"))
        _add_candidate(candidates, source.get("user_id"))
        _add_candidate(candidates, source.get("author_id"))
    if isinstance(raw, dict):
        _add_candidate(candidates, extract_dialog_id(raw))
        author_id = extract_author_id(raw)
        if author_id and author_id != BOT_ID:
            _add_candidate(candidates, author_id)
        chat_id = extract_chat_id(raw)
        chat_type = ""
        _d, _m, chat, _p = _event_data(raw)
        if isinstance(chat, dict):
            chat_type = str(chat.get("type") or chat.get("messageType") or "").lower()
        if chat_id and chat_type not in {"private", "p"}:
            _add_candidate(candidates, f"chat{chat_id}")

    if dialog_id and not str(dialog_id).startswith("chat"):
        meta = index_bot_chat(dialog_id, source="download_candidates")
        if meta:
            _add_candidate(candidates, meta.get("dialog_id"))
    return candidates


def get_download_url_any(dialog_id, file_id, source=None):
    source = source or {}
    direct_url = source.get("download_url") or source.get("DOWNLOAD_URL")
    if isinstance(direct_url, str) and direct_url.startswith("http"):
        return direct_url, source.get("download_dialog") or dialog_id, "direct"

    errors = []
    for candidate in dialog_download_candidates(dialog_id, source):
        try:
            url = get_download_url_by_file_id(candidate, file_id)
            if url:
                return url, candidate, "imbot"
            errors.append({"dialog_id": candidate, "error": "empty_download_url"})
        except Exception as e:
            errors.append({"dialog_id": candidate, "error": short_text(e, 500)})

    try:
        url = disk_file_download_url(file_id)
        if url:
            return url, dialog_id, "disk.file.get"
    except Exception as e:
        errors.append({"dialog_id": "disk.file.get", "error": short_text(e, 500)})

    append_jsonl(FILE_DOWNLOAD_FAILURES_LOG, {
        "event": "download_url_failed",
        "file_id": file_id,
        "dialog_id": str(dialog_id) if dialog_id is not None else None,
        "user_id": source.get("user_id") if isinstance(source, dict) else None,
        "errors": errors,
    })
    return None, None, None


def suffix_from_name(name):
    suffix = Path(str(name or "")).suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        return suffix
    return ".xlsx"


def download_excel_to_temp(download_url, suffix=".xlsx"):
    r = requests.get(download_url, timeout=60, stream=True)
    r.raise_for_status()
    content_length = safe_int(r.headers.get("Content-Length"))
    if content_length and content_length > MAX_UPLOAD_BYTES:
        raise ValueError(f"file_too_large: {content_length} bytes > limit {MAX_UPLOAD_BYTES}")
    total = 0
    tmp_name = None
    with NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp_name = tmp.name
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            if chunk:
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    tmp.close()
                    Path(tmp_name).unlink(missing_ok=True)
                    raise ValueError(f"file_too_large: {total} bytes > limit {MAX_UPLOAD_BYTES}")
                tmp.write(chunk)
        return tmp_name


# ───────────────────────── core logic ───────────────────────────────────────

def send_one(session, token, sklad_id, nakl, timeout):
    headers = {"token": token}
    payload = {"cdek_number": nakl, "sklad_id": sklad_id}
    return session.post(URL, headers=headers, json=payload, timeout=timeout)


def classify_ff_response(resp):
    body = short_text(resp.text, 1000)
    body_l = body.lower()
    already_exists = (
        resp.status_code in {400, 409}
        and ("уже существует" in body_l or "already exists" in body_l or "already exist" in body_l)
    )
    if already_exists:
        return "already_exists", True, body

    if not (200 <= resp.status_code < 300):
        return "failed", False, body

    try:
        data = resp.json()
    except ValueError:
        return "sent", True, body

    if isinstance(data, dict):
        if data.get("success") is False or data.get("error") or data.get("errors"):
            return "failed", False, body
        status = str(data.get("status", "")).lower()
        if status in {"error", "failed", "fail"}:
            return "failed", False, body

    return "sent", True, body


def process_one_nakl(session, batch_base, nakl, seq, total, already_sent):
    nakl_lock = acquire_blocking_lock("sent-nakls")
    try:
        current_sent = set() if FORCE else load_sent_log(SENT_LOG)
        if not FORCE and nakl in current_sent:
            body = "already present in sent log at send time"
            print(f"[{seq}/{total}] {nakl} -> SKIP_ALREADY_SENT: {body}")
            append_batch_item({
                **batch_base,
                "nakl": nakl,
                "seq": seq,
                "total": total,
                "attempt": 0,
                "status": "already_sent_runtime",
                "ok": True,
                "response": body,
                "duration_ms": 0,
            })
            already_sent.add(nakl)
            return "already_sent_runtime", True

        for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
            started = time.monotonic()
            try:
                if DRY_RUN:
                    body = "dry run: request skipped"
                    print(f"[{seq}/{total}] {nakl} -> DRY_RUN: {body}")
                    append_batch_item({
                        **batch_base,
                        "nakl": nakl,
                        "seq": seq,
                        "total": total,
                        "attempt": attempt,
                        "status": "dry_run",
                        "ok": True,
                        "response": body,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                    })
                    return "dry_run", True

                resp = send_one(session, FF_TOKEN, SKLAD_ID, nakl, TIMEOUT)
                status, is_ok, body = classify_ff_response(resp)
                body_short = short_text(body, 300)

                if status == "sent":
                    print(f"[{seq}/{total}] {nakl} -> OK HTTP {resp.status_code}: {body_short}")
                elif status == "already_exists":
                    print(f"[{seq}/{total}] {nakl} -> ALREADY_EXISTS HTTP {resp.status_code}: {body_short}")
                else:
                    print(f"[{seq}/{total}] {nakl} -> HTTP {resp.status_code}: {body_short}")

                append_batch_item({
                    **batch_base,
                    "nakl": nakl,
                    "seq": seq,
                    "total": total,
                    "attempt": attempt,
                    "status": status,
                    "http_status": resp.status_code,
                    "ok": is_ok,
                    "response": body,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                })

                if is_ok:
                    if nakl not in current_sent:
                        append_sent_log(SENT_LOG, nakl)
                        current_sent.add(nakl)
                        already_sent.add(nakl)
                    return status, True

                if resp.status_code in RETRY_STATUS_CODES and attempt < MAX_SEND_ATTEMPTS:
                    wait = RETRY_BASE_DELAY * attempt
                    print(f"[{seq}/{total}] {nakl} -> retry {attempt + 1}/{MAX_SEND_ATTEMPTS} через {wait:.0f} сек")
                    time.sleep(wait)
                    continue

                append_failed_nakl({
                    **batch_base,
                    "nakl": nakl,
                    "seq": seq,
                    "status": status,
                    "http_status": resp.status_code,
                    "response": body,
                })
                return status, False

            except requests.RequestException as e:
                error_text = short_text(e, 1000)
                append_batch_item({
                    **batch_base,
                    "nakl": nakl,
                    "seq": seq,
                    "total": total,
                    "attempt": attempt,
                    "status": "request_error",
                    "ok": False,
                    "error": error_text,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                })
                if attempt < MAX_SEND_ATTEMPTS:
                    wait = RETRY_BASE_DELAY * attempt
                    print(f"[{seq}/{total}] {nakl} -> ERROR: {short_text(e, 500)}; retry {attempt + 1}/{MAX_SEND_ATTEMPTS} через {wait:.0f} сек")
                    time.sleep(wait)
                    continue
                print(f"[{seq}/{total}] {nakl} -> ERROR: {short_text(e, 500)}")
                append_failed_nakl({
                    **batch_base,
                    "nakl": nakl,
                    "seq": seq,
                    "status": "request_error",
                    "error": error_text,
                })
                return "request_error", False
    finally:
        try:
            nakl_lock.close()
        except Exception:
            pass
    return "failed", False


def run_exact_logic(file_path, *, file_id=None, dialog_id=None, file_hash=None):
    batch_id = f"{int(time.time())}-{file_id or 'manual'}-{uuid.uuid4().hex[:8]}"
    batch_base = {
        "batch_id": batch_id,
        "file_id": file_id,
        "dialog_id": str(dialog_id) if dialog_id is not None else None,
        "file_hash": file_hash,
        "file_name": Path(file_path).name,
        "sklad_id": SKLAD_ID,
        "dry_run": DRY_RUN,
        "force": FORCE,
    }
    append_batch_run({**batch_base, "event": "started"})
    print(f"Batch ID: {batch_id}")

    if not FF_TOKEN:
        print("Ошибка: укажите token через переменную окружения FF_TOKEN")
        append_batch_run({**batch_base, "event": "finished", "status": "failed", "error": "missing_ff_token"})
        return False
    if not file_path.lower().endswith((".xlsx", ".xlsm")):
        print("Поддерживаются .xlsx/.xlsm. Если у вас .xls, пересохраните файл в .xlsx")
        append_batch_run({**batch_base, "event": "finished", "status": "failed", "error": "unsupported_file_type"})
        return False

    nakls = read_nakls_from_excel(
        path=file_path,
        sheet_name=SHEET_NAME,
        column=COLUMN,
        skip_header=SKIP_HEADER,
    )
    if not nakls:
        print("В файле не найдено накладных.")
        append_batch_run({**batch_base, "event": "finished", "status": "noop", "unique_count": 0})
        return True

    already_sent = set() if FORCE else load_sent_log(SENT_LOG)
    to_send = [n for n in nakls if n not in already_sent]
    skipped_count = len(nakls) - len(to_send)

    print(f"Найдено уникальных в Excel: {len(nakls)}")
    if not FORCE:
        print(f"Уже есть в журнале: {skipped_count}")
    print(f"К отправке: {len(to_send)}")
    if DRY_RUN:
        print("DRY RUN: реальные запросы в FF не отправляются, журналы отправленных не меняются.")

    append_batch_run({
        **batch_base,
        "event": "parsed",
        "unique_count": len(nakls),
        "already_sent_count": skipped_count,
        "to_send_count": len(to_send),
    })

    if not to_send:
        print("Нечего отправлять.")
        append_batch_run({
            **batch_base,
            "event": "finished",
            "status": "noop",
            "unique_count": len(nakls),
            "already_sent_count": skipped_count,
            "to_send_count": 0,
        })
        return True

    session = requests.Session()
    ok_count = 0
    already_exists_count = 0
    err_count = 0
    runtime_skipped_count = 0

    for i, nakl in enumerate(to_send, start=1):
        status, final_ok = process_one_nakl(session, batch_base, nakl, i, len(to_send), already_sent)
        if final_ok:
            if status == "already_exists":
                already_exists_count += 1
            elif status == "already_sent_runtime":
                runtime_skipped_count += 1
            else:
                ok_count += 1
        else:
            err_count += 1

        if DELAY > 0 and i < len(to_send) and not DRY_RUN:
            time.sleep(DELAY)

    print()
    print(f"Готово. Успешно создано: {ok_count}, уже было в FF: {already_exists_count}, пропущено как уже отправленные: {runtime_skipped_count}, ошибок: {err_count}")
    print(f"Журнал отправленных: {SENT_LOG}")
    print(f"Аудит партии: {BATCH_RUNS_LOG}, строки: {BATCH_ITEMS_LOG}")
    append_batch_run({
        **batch_base,
        "event": "finished",
        "status": "success" if err_count == 0 else "partial_failed",
        "unique_count": len(nakls),
        "already_sent_count": skipped_count,
        "to_send_count": len(to_send),
        "created_count": ok_count,
        "already_exists_count": already_exists_count,
        "runtime_skipped_count": runtime_skipped_count,
        "error_count": err_count,
    })
    return err_count == 0


# ───────────────────────── file processing ──────────────────────────────────

def _do_process_file(dialog_id, file_id, source=None):
    """Реальная обработка файла — вызывается после debounce."""
    file_id_str = str(file_id)
    source = source or {}
    job_dialog_id = dialog_id
    queue_key = make_job_key(dialog_id, file_id)

    # In-memory защита: если уже обрабатывается прямо сейчас — выходим
    with _active_lock:
        if queue_key in _active:
            print(f"[skip] job={queue_key} уже обрабатывается, пропускаю")
            return
        _active.add(queue_key)

    file_lock = None
    tmp_path = None
    try:
        append_file_job("started", job_dialog_id, file_id, source=source)
        # Файловый лок: защита от двух экземпляров процесса
        file_lock = acquire_file_lock(f"file-{file_id}")
        if file_lock is None:
            print(f"[debounce] file_id={file_id} уже обрабатывается другим процессом, пропускаю.", flush=True)
            return

        # Проверяем file_id. Отмечаем обработанным только после успешного прохода.
        processed_files = load_simple_log(PROCESSED_FILES_LOG)
        if file_id_str in processed_files:
            append_file_job("completed", job_dialog_id, file_id, source=source, reason="already_processed_file_id")
            return

        download_url, download_dialog, download_source = get_download_url_any(dialog_id, file_id, source)
        if not download_url:
            safe_send_bot_message(dialog_id, f"Не удалось получить ссылку на файл (FILE_ID={file_id})")
            append_file_job("retryable_failed", job_dialog_id, file_id, source=source, reason="download_url_not_found")
            return
        if download_dialog and str(download_dialog) != str(dialog_id):
            print(
                f"[download] file_id={file_id} resolved dialog {dialog_id} -> {download_dialog} via {download_source}",
                flush=True,
            )
            dialog_id = download_dialog
            remember_dialog(dialog_id)

        tmp_path = download_excel_to_temp(download_url, suffix=suffix_from_name(source.get("file_name")))

        processed_hashes = load_simple_log(PROCESSED_HASHES_LOG)
        fhash = file_sha256(tmp_path)
        if fhash in processed_hashes:
            safe_send_bot_message(dialog_id, "Файл уже встречался ранее, проверяю по журналу отправленных номеров...")

        safe_send_bot_message(dialog_id, "Файл получен, начинаю обработку...")

        buf_out, buf_err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            processed_ok = run_exact_logic(tmp_path, file_id=file_id, dialog_id=dialog_id, file_hash=fhash)

        lines = [x for x in buf_out.getvalue().splitlines() if x.strip()]
        lines += [x for x in buf_err.getvalue().splitlines() if x.strip()]
        for line in (lines or ["Скрипт завершился без вывода."]):
            print(f"[batch file_id={file_id}] {line}", flush=True)
        safe_send_bot_lines(dialog_id, lines or ["Скрипт завершился без вывода."])

        if processed_ok:
            append_simple_log(PROCESSED_FILES_LOG, file_id_str)
            if fhash not in processed_hashes:
                append_simple_log(PROCESSED_HASHES_LOG, fhash)
            append_file_job("completed", job_dialog_id, file_id, source=source)
        else:
            append_simple_log(PROCESSED_FILES_LOG, file_id_str)
            if fhash not in processed_hashes:
                append_simple_log(PROCESSED_HASHES_LOG, fhash)
            safe_send_bot_message(
                dialog_id,
                f"Партия FILE_ID={file_id} обработана, но часть накладных не прошла. "
                f"Автоповтор отключен; подробности в {BATCH_ITEMS_LOG}.",
            )
            append_file_job("terminal_failed", job_dialog_id, file_id, source=source, reason="processing_finished_with_item_errors")

    except Exception as e:
        print(f"Ошибка обработки FILE_ID={file_id}: {short_text(e, 500)}", flush=True)
        terminal = isinstance(e, (ValueError, BadZipFile, InvalidFileException))
        reason = short_text(e, 1000)
        append_failed_file(job_dialog_id, file_id, reason, source=source, terminal=terminal)
        append_file_job("terminal_failed" if terminal else "retryable_failed", job_dialog_id, file_id, source=source, reason=reason)
        try:
            safe_send_bot_message(dialog_id, f"Ошибка обработки файла: {short_text(e, 500)}")
        except Exception as notify_error:
            print(f"Не удалось отправить ошибку в Bitrix: {short_text(notify_error, 500)}", flush=True)
    finally:
        with _active_lock:
            _active.discard(queue_key)
        if file_lock is not None:
            try:
                file_lock.close()
            except Exception:
                pass
        if tmp_path and Path(tmp_path).exists():
            Path(tmp_path).unlink(missing_ok=True)
        with _debounce_lock:
            _debounce.pop(queue_key, None)

def _process_with_timeout(dialog_id, file_id, source=None):
    """Обрабатывает файл после debounce.

    Жесткий timeout убран: раньше бот писал, что обработка прервана, но daemon-поток
    продолжал отправлять запросы в FF. Теперь timeout используется только как сигнал
    в отчете, а партия всегда доходит до нормального финального статуса.
    """
    started = time.monotonic()
    _do_process_file(dialog_id, file_id, source=source)
    elapsed = int(time.monotonic() - started)
    if elapsed > FILE_PROCESS_TIMEOUT:
        safe_send_bot_message(
            dialog_id,
            f"Обработка FILE_ID={file_id} заняла {elapsed} сек. Это дольше ориентира {FILE_PROCESS_TIMEOUT} сек, но партия была доведена до финального статуса.",
        )


# ───────────────────────── debounce ─────────────────────────────────────────

def schedule_file(dialog_id, file_id, source=None, persist=True):
    """
    Каждый раз когда Bitrix присылает событие с file_id —
    сбрасываем таймер. Реальная обработка стартует только после
    DEBOUNCE_SECONDS секунд тишины.
    """
    if _shutdown_requested.is_set():
        print(f"[shutdown] file_id={file_id} не ставлю в очередь: сервис завершает работу", flush=True)
        return

    if is_job_terminal(dialog_id, file_id):
        print(f"[skip] job={make_job_key(dialog_id, file_id)} уже завершен/terminal, пропускаю", flush=True)
        return
    job_key = make_job_key(dialog_id, file_id)
    source = source or {}
    if persist:
        append_file_job("queued", dialog_id, file_id, source=source)

    with _debounce_lock:
        existing = _debounce.get(job_key)
        if existing:
            existing["timer"].cancel()   # сбрасываем старый таймер

        timer = threading.Timer(
            DEBOUNCE_SECONDS,
            _process_with_timeout,
            args=(dialog_id, file_id, source),
        )
        _debounce[job_key] = {"dialog_id": dialog_id, "timer": timer, "source": source}
        timer.start()


# ───────────────────────── event handler ────────────────────────────────────

def handle_message_event(ev):
    user_id = extract_author_id(ev)
    dialog_id = extract_dialog_id(ev)
    chat_id = extract_chat_id(ev)
    file_ids = extract_file_ids(ev)
    append_jsonl(INCOMING_EVENTS_LOG, {
        "event": "incoming",
        "event_type": ev.get("type") or ev.get("event") or ev.get("EVENT"),
        "user_id": user_id,
        "dialog_id": str(dialog_id) if dialog_id else None,
        "chat_id": chat_id,
        "file_ids": file_ids,
        "brief": event_brief(ev),
        "raw": ev,
    })

    if user_id == BOT_ID:
        print(f"[{time.strftime('%H:%M:%S')}] SKIP self {event_brief(ev)}", flush=True)
        return

    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        print(f"[{time.strftime('%H:%M:%S')}] SKIP unauthorized {event_brief(ev)}", flush=True)
        return

    if not dialog_id:
        print(f"[{time.strftime('%H:%M:%S')}] WARN no_dialog {event_brief(ev)}", flush=True)
        return

    remember_dialog(dialog_id)
    if chat_id:
        remember_chat_id(chat_id)
    index_bot_chat(dialog_id, source="incoming_event")

    if not file_ids:
        print(f"[{time.strftime('%H:%M:%S')}] SKIP no_file {event_brief(ev)}", flush=True)
        if user_id not in {None, 0, BOT_ID}:
            key = str(dialog_id)
            now = time.monotonic()
            if now >= _no_file_reply_until.get(key, 0):
                safe_send_bot_message(dialog_id, "Я жду Excel-файл с накладными. Пришлите файл .xlsx, и я загружу его в FF.")
                _no_file_reply_until[key] = now + NO_FILE_REPLY_TTL_SECONDS
        return

    for file_id in file_ids:
        print(f"[{time.strftime('%H:%M:%S')}] СОБЫТИЕ file_id={file_id} user={user_id or 'unknown'} dialog={dialog_id}", flush=True)
        schedule_file(dialog_id, file_id, source={
            "source": "incoming_event",
            "dialog_id": str(dialog_id),
            "user_id": user_id,
            "chat_id": chat_id,
            "raw": ev,
        })


# ───────────────────────── main loop ────────────────────────────────────────

def main():
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    if not REST_BASE or not BOT_ID or not BOT_TOKEN:
        raise RuntimeError("BITRIX_REST_BASE, BITRIX_BOT_ID and BITRIX_BOT_TOKEN must be set")

    print(f"Bot started. Debounce={DEBOUNCE_SECONDS}s, timeout={FILE_PROCESS_TIMEOUT}s.")
    first_user_events_enable = ensure_user_event_subscription()
    recover_pending_file_jobs()
    offset = load_offset()
    if first_user_events_enable:
        print("[events] first user-events enable: starting next poll from zero offset once", flush=True)
        offset = 0
    poll_delay = POLL_SECONDS
    global _last_history_scan_at, _last_zero_offset_probe_at, _last_bot_chat_scan_at

    while not _shutdown_requested.is_set() or active_count() > 0:
        if _shutdown_requested.is_set():
            print(f"Shutdown pending: active file processors={active_count()}", flush=True)
            time.sleep(5)
            continue

        try:
            has_more = True
            while has_more and not _shutdown_requested.is_set():
                events, next_offset, has_more = get_events(offset)
                now_probe = time.monotonic()
                if not events and offset > 0 and now_probe - _last_zero_offset_probe_at >= OFFSET_RESET_PROBE_SECONDS:
                    _last_zero_offset_probe_at = now_probe
                    probe_events, probe_next_offset, probe_has_more = get_events(0)
                    if probe_events:
                        print(
                            f"[offset] recovered {len(probe_events)} event(s) with zero-offset probe; "
                            f"old_offset={offset} probe_next={probe_next_offset}",
                            flush=True,
                        )
                        events, next_offset, has_more = probe_events, probe_next_offset, probe_has_more
                        offset = 0
                for ev in events:
                    if _shutdown_requested.is_set():
                        break
                    if is_message_event(ev) or extract_file_ids(ev):
                        handle_message_event(ev)
                    else:
                        print(f"[{time.strftime('%H:%M:%S')}] SKIP non_file_event {event_brief(ev)}", flush=True)
                offset = next_offset
                save_offset(offset)
            now = time.monotonic()
            recover_pending_file_jobs()
            if now - _last_history_scan_at >= HISTORY_SCAN_SECONDS:
                recover_visible_history_files()
                _last_history_scan_at = now
            if now - _last_bot_chat_scan_at >= BOT_CHAT_SCAN_SECONDS:
                recover_bot_chat_disk_files()
                _last_bot_chat_scan_at = now
            poll_delay = POLL_SECONDS

        except Exception as e:
            print(f"Polling error: {short_text(e, 500)}", flush=True)
            poll_delay = min(poll_delay * 2, 60)

        for _ in range(int(poll_delay)):
            if _shutdown_requested.is_set():
                break
            time.sleep(1)
        if poll_delay != int(poll_delay) and not _shutdown_requested.is_set():
            time.sleep(poll_delay - int(poll_delay))

    print("Bot stopped gracefully.", flush=True)


if __name__ == "__main__":
    main()
