import asyncio
import json
import logging
import os
import sqlite3
from datetime import date, datetime, timezone, timedelta

import httpx

from app.config import settings
from app.database import Base, DATABASE_URL, DB_PATH, engine
from app.media_security import MEDIA_DIR

logger = logging.getLogger(__name__)

BACKUP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "backups")


def _backup_name() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    suffix = "db" if DATABASE_URL.startswith("sqlite") else "json"
    return f"app-{stamp}.{suffix}"


def list_backups() -> list[dict]:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    rows = []
    for name in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if not (name.endswith(".db") or name.endswith(".json")):
            continue
        path = os.path.join(BACKUP_DIR, name)
        stat = os.stat(path)
        rows.append({
            "name": name,
            "size": stat.st_size,
            "created_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        })
    return rows


def _json_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _create_sqlite_backup() -> dict:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = _backup_name()
    target = os.path.join(BACKUP_DIR, name)
    source = sqlite3.connect(DB_PATH)
    try:
        dest = sqlite3.connect(target)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    stat = os.stat(target)
    return {"name": name, "path": target, "size": stat.st_size, "created_at": datetime.now(timezone.utc).isoformat()}


async def _create_json_backup() -> dict:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = _backup_name()
    target = os.path.join(BACKUP_DIR, name)
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database": "postgresql" if "postgresql" in DATABASE_URL else DATABASE_URL.split(":", 1)[0],
        "tables": {},
    }
    async with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            result = await conn.execute(table.select().order_by(table.c.id))
            payload["tables"][table.name] = [
                {key: _json_value(value) for key, value in dict(row).items()}
                for row in result.mappings()
            ]
    with open(target, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    stat = os.stat(target)
    return {"name": name, "path": target, "size": stat.st_size, "created_at": payload["created_at"]}


async def create_backup_async() -> dict:
    if DATABASE_URL.startswith("sqlite"):
        return await asyncio.to_thread(_create_sqlite_backup)
    return await _create_json_backup()


def create_backup() -> dict:
    if DATABASE_URL.startswith("sqlite"):
        return _create_sqlite_backup()
    return asyncio.run(_create_json_backup())


def cleanup_backups(retention_days: int | None = None) -> int:
    retention_days = retention_days if retention_days is not None else settings.backup_retention_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(retention_days, 1))
    removed = 0
    for item in list_backups():
        try:
            created = datetime.fromisoformat(item["created_at"])
        except Exception:
            continue
        if created < cutoff:
            os.remove(os.path.join(BACKUP_DIR, item["name"]))
            removed += 1
    return removed


def cleanup_media_files(retention_seconds: int | None = None) -> dict:
    retention_seconds = retention_seconds if retention_seconds is not None else settings.media_retention_seconds
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(int(retention_seconds or 0), 3600))
    removed = 0
    skipped = 0
    bytes_removed = 0
    os.makedirs(MEDIA_DIR, exist_ok=True)
    for name in os.listdir(MEDIA_DIR):
        path = os.path.join(MEDIA_DIR, name)
        if not os.path.isfile(path):
            skipped += 1
            continue
        try:
            stat = os.stat(path)
            modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
            if modified >= cutoff:
                continue
            os.remove(path)
            removed += 1
            bytes_removed += stat.st_size
        except FileNotFoundError:
            continue
        except Exception as exc:
            skipped += 1
            logger.warning("Media cleanup skipped %s: %s", name, exc)
    return {
        "removed": removed,
        "skipped": skipped,
        "bytes_removed": bytes_removed,
        "retention_seconds": int(retention_seconds or 0),
    }


async def send_health_notification(text: str, payload: dict | None = None) -> bool:
    sent = False
    payload = payload or {}
    async with httpx.AsyncClient(timeout=15) as client:
        if settings.health_alert_webhook_url:
            try:
                await client.post(settings.health_alert_webhook_url, json={"text": text, "payload": payload})
                sent = True
            except Exception as exc:
                logger.warning("Health webhook notification failed: %s", exc)
        if settings.health_alert_telegram_bot_token and settings.health_alert_telegram_chat_id:
            try:
                await client.post(
                    f"https://api.telegram.org/bot{settings.health_alert_telegram_bot_token}/sendMessage",
                    json={
                        "chat_id": settings.health_alert_telegram_chat_id,
                        "text": text,
                        "disable_web_page_preview": True,
                    },
                )
                sent = True
            except Exception as exc:
                logger.warning("Health Telegram notification failed: %s", exc)
    return sent


async def backup_scheduler():
    last_media_cleanup_at: datetime | None = None
    while True:
        try:
            backups = list_backups()
            today = datetime.now(timezone.utc).strftime("%Y%m%d")
            latest_created = None
            for item in backups:
                try:
                    created = datetime.fromisoformat(item["created_at"])
                except Exception:
                    continue
                if latest_created is None or created > latest_created:
                    latest_created = created
            min_interval_ok = (
                latest_created is None or
                (datetime.now(timezone.utc) - latest_created).total_seconds() >= settings.min_backup_interval_seconds
            )
            if min_interval_ok and not any(item["name"].startswith(f"app-{today}") for item in backups):
                created = await create_backup_async()
                removed = await asyncio.to_thread(cleanup_backups)
                logger.info("Backup created: %s; old removed=%s", created["name"], removed)
            now = datetime.now(timezone.utc)
            cleanup_interval = max(int(settings.media_cleanup_interval_seconds or 0), 3600)
            if (
                last_media_cleanup_at is None or
                (now - last_media_cleanup_at).total_seconds() >= cleanup_interval
            ):
                media_removed = await asyncio.to_thread(cleanup_media_files)
                last_media_cleanup_at = now
                if media_removed["removed"]:
                    logger.info(
                        "Media cleanup removed=%s bytes=%s retention=%ss",
                        media_removed["removed"],
                        media_removed["bytes_removed"],
                        media_removed["retention_seconds"],
                    )
        except Exception as exc:
            logger.error("Backup scheduler error: %s", exc)
        await asyncio.sleep(3600)


async def health_monitor():
    last_status: dict[str, str] = {}
    await asyncio.sleep(20)
    while True:
        try:
            from sqlalchemy import select
            from app.database import async_session, Tenant
            from app.api.admin import admin_health
            from app.admin_security import write_admin_audit

            async with async_session() as session:
                tenants = (await session.execute(select(Tenant))).scalars().all()
            for tenant in tenants:
                health = await admin_health(domain=tenant.bitrix_domain)
                status = health.get("overall", "error") if health.get("success") else "error"
                previous = last_status.get(tenant.bitrix_domain)
                if previous != status:
                    last_status[tenant.bitrix_domain] = status
                    if status != "ok" or previous:
                        checks = ", ".join(
                            f"{item.get('code')}={item.get('status')}"
                            for item in health.get("checks", [])
                            if item.get("status") != "ok"
                        ) or "all ok"
                        text = f"SAFY Telegram health {tenant.bitrix_domain}: {status}. {checks}"
                        sent = False
                        try:
                            sent = await send_health_notification(text, health)
                        except Exception as notify_exc:
                            logger.warning("Health notification failed: %s", notify_exc)
                        await write_admin_audit(
                            actor="system",
                            ip="",
                            method="MONITOR",
                            path="/api/admin/health",
                            status_code=200,
                            action="health_status_change",
                            details={"domain": tenant.bitrix_domain, "status": status, "sent": sent},
                        )
        except Exception as exc:
            logger.error("Health monitor error: %s", exc)
        await asyncio.sleep(max(settings.health_check_interval_seconds, 60))
