#!/usr/bin/env python3
import base64
import hashlib
import os
import secrets
import shutil
import sqlite3
from pathlib import Path

from cryptography.fernet import Fernet


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "app.db"
ENV_PATH = ROOT / ".env"


def _derive(key_text: str) -> bytes:
    return base64.urlsafe_b64encode(hashlib.sha256(key_text.encode()).digest())


def _load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_PATH.exists():
        return values
    for line in ENV_PATH.read_text().splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _write_env(values: dict[str, str], updates: dict[str, str]) -> None:
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    seen = set()
    new_lines = []
    for line in lines:
        if not line or line.lstrip().startswith("#") or "=" not in line:
            new_lines.append(line)
            continue
        key, _ = line.split("=", 1)
        key = key.strip()
        if key in updates:
            new_lines.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            new_lines.append(line)
    for key, value in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(new_lines) + "\n")


def _decrypt_or_plain(value: str | None, fernet: Fernet) -> str | None:
    if value is None:
        return None
    try:
        return fernet.decrypt(value.encode()).decode()
    except Exception:
        return value


def _encrypt(value: str | None, fernet: Fernet) -> str | None:
    if value is None:
        return None
    return fernet.encrypt(value.encode()).decode()


def main() -> None:
    env = _load_env()
    old_key_text = env.get("ENCRYPTION_KEY", "")
    if not old_key_text:
        raise SystemExit("ENCRYPTION_KEY is missing in .env")
    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}")

    new_key_text = os.environ.get("NEW_ENCRYPTION_KEY") or secrets.token_urlsafe(48)
    media_secret = os.environ.get("NEW_MEDIA_URL_SECRET") or env.get("MEDIA_URL_SECRET") or secrets.token_urlsafe(48)
    old_fernet = Fernet(_derive(old_key_text))
    new_fernet = Fernet(_derive(new_key_text))

    backup = DB_PATH.with_suffix(".db.before-key-rotation")
    shutil.copy2(DB_PATH, backup)

    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, access_token_encrypted, refresh_token_encrypted FROM tenants")
        for row_id, access_token, refresh_token in cur.fetchall():
            cur.execute(
                "UPDATE tenants SET access_token_encrypted=?, refresh_token_encrypted=? WHERE id=?",
                (
                    _encrypt(_decrypt_or_plain(access_token, old_fernet), new_fernet),
                    _encrypt(_decrypt_or_plain(refresh_token, old_fernet), new_fernet),
                    row_id,
                ),
            )

        cur.execute("SELECT id, api_hash, session_string FROM telegram_accounts")
        for row_id, api_hash, session_string in cur.fetchall():
            cur.execute(
                "UPDATE telegram_accounts SET api_hash=?, session_string=? WHERE id=?",
                (
                    _encrypt(_decrypt_or_plain(api_hash, old_fernet), new_fernet),
                    _encrypt(_decrypt_or_plain(session_string, old_fernet), new_fernet),
                    row_id,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        shutil.copy2(backup, DB_PATH)
        raise
    finally:
        conn.close()

    _write_env(env, {
        "ENCRYPTION_KEY": new_key_text,
        "MEDIA_URL_SECRET": media_secret,
    })
    print(f"Rotated encryption key. Backup: {backup}")


if __name__ == "__main__":
    main()
