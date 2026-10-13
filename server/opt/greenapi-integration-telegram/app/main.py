import logging
import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from app.api.routes import router
from app.api.admin import router as admin_router
from app.api.telegram_routes import router as tg_router
from app.database import init_db
from app.config import settings
from app.logging_config import setup_logging
from app.outbox import outbox
from app.openline_poller import openline_pending_poller
from app.admin_security import admin_auth_middleware
from app.maintenance import backup_scheduler, health_monitor
from app.media_security import safe_media_path, verify_media_token

logger = setup_logging()

MEDIA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "media")
os.makedirs(MEDIA_DIR, exist_ok=True)


async def outbox_processor():
    try:
        await outbox.recover_stale_processing()
    except Exception as e:
        logger.error(f"Outbox stale recovery error: {e}")
    while True:
        try:
            await outbox.process_next()
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Outbox processor error: {e}")
            await asyncio.sleep(5)


async def token_refresher():
    """Refresh Bitrix24 access tokens shortly before they expire."""
    await asyncio.sleep(5)
    while True:
        try:
            from app.database import async_session, Tenant
            from app.crypto import decrypt_token, encrypt_token
            from sqlalchemy import select, update
            import httpx

            async with async_session() as session:
                result = await session.execute(select(Tenant))
                tenants = result.scalars().all()

                for tenant in tenants:
                    try:
                        now = datetime.now(timezone.utc)
                        expires_at = tenant.expires_at
                        if expires_at and expires_at.tzinfo is None:
                            expires_at = expires_at.replace(tzinfo=timezone.utc)
                        if expires_at and expires_at > now + timedelta(minutes=10):
                            continue

                        refresh = decrypt_token(tenant.refresh_token_encrypted)
                        if not refresh:
                            continue

                        async with httpx.AsyncClient() as client:
                            r = await client.post(
                                f"https://{tenant.bitrix_domain}/oauth/token/",
                                data={
                                    "grant_type": "refresh_token",
                                    "client_id": settings.bitrix24_client_id,
                                    "client_secret": settings.bitrix24_client_secret,
                                    "refresh_token": refresh,
                                },
                                timeout=10,
                            )
                            data = r.json()

                        if "access_token" in data:
                            new_token = encrypt_token(data["access_token"])
                            new_refresh = encrypt_token(data["refresh_token"]) if "refresh_token" in data else tenant.refresh_token_encrypted
                            try:
                                new_expires_at = now + timedelta(seconds=int(data.get("expires_in") or 3600))
                            except (TypeError, ValueError):
                                new_expires_at = now + timedelta(hours=1)
                            new_expires_at = new_expires_at.replace(tzinfo=None)
                            await session.execute(
                                update(Tenant).where(Tenant.id == tenant.id).values(
                                    access_token_encrypted=new_token,
                                    refresh_token_encrypted=new_refresh,
                                    expires_at=new_expires_at,
                                )
                            )
                            await session.commit()
                            logger.info(f"Token refreshed for {tenant.bitrix_domain}")
                        else:
                            logger.error(f"Token refresh failed for {tenant.bitrix_domain}: {data.get('error_description', data.get('error', 'unknown'))}")
                    except Exception as e:
                        logger.error(f"Token refresh error for {tenant.bitrix_domain}: {e}")
        except Exception as e:
            logger.error(f"Token refresher error: {e}")

        await asyncio.sleep(3000)  # 50 minutes


async def telegram_watchdog():
    """Monitor Telegram clients and restart if disconnected."""
    while True:
        try:
            await asyncio.sleep(30)
            from app.telegram.service import _active_clients, _listener_tasks, start_listener_task
            from app.database import async_session, TelegramAccount
            from app.api.telegram_routes import _telegram_inbound_callback
            from app.crypto import decrypt_if_encrypted
            from sqlalchemy import select as sa_select

            async with async_session() as session:
                result = await session.execute(
                    sa_select(TelegramAccount).where(TelegramAccount.state.in_(["active", "authorized"]))
                )
                accounts = result.scalars().all()

                for acc in accounts:
                    task = _listener_tasks.get(acc.id)
                    if task and not task.done():
                        continue

                    client = _active_clients.get(acc.id)
                    if client and client.is_connected():
                        continue

                    if client:
                        try:
                            await client.disconnect()
                        except Exception:
                            pass
                        _active_clients.pop(acc.id, None)

                    try:
                        start_listener_task(
                            account_id=acc.id,
                            session_string=decrypt_if_encrypted(acc.session_string),
                            api_id=acc.api_id,
                            api_hash=decrypt_if_encrypted(acc.api_hash),
                            on_message_callback=_telegram_inbound_callback,
                        )
                        logger.info(f"Watchdog: restarted listener for account {acc.id}")
                    except Exception as e:
                        logger.error(f"Watchdog: failed to restart account {acc.id}: {e}")
        except Exception as e:
            logger.error(f"Watchdog error: {e}")
            await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting SAFY Telegram connector...")
    await init_db()
    logger.info("Database initialized")

    processor_task = asyncio.create_task(outbox_processor())
    logger.info("Outbox processor started")

    token_task = asyncio.create_task(token_refresher())
    logger.info("Token refresher started (checks expiry every 50 min)")

    watchdog_task = asyncio.create_task(telegram_watchdog())
    logger.info("Telegram watchdog started (every 30 sec)")

    openline_poller_task = asyncio.create_task(openline_pending_poller())
    logger.info("OpenLine pending-message poller started (every 10 sec)")

    backup_task = asyncio.create_task(backup_scheduler())
    logger.info("Backup scheduler started")

    health_task = asyncio.create_task(health_monitor())
    logger.info("Health monitor started")

    try:
        from app.database import async_session, TelegramAccount
        from app.telegram.service import start_listener_task
        from app.api.telegram_routes import _telegram_inbound_callback
        from app.crypto import decrypt_if_encrypted
        from sqlalchemy import select as sa_select

        async with async_session() as session:
            result = await session.execute(
                sa_select(TelegramAccount).where(TelegramAccount.state.in_(["active", "authorized"]))
            )
            accounts = result.scalars().all()
            for acc in accounts:
                try:
                    start_listener_task(
                        account_id=acc.id,
                        session_string=decrypt_if_encrypted(acc.session_string),
                        api_id=acc.api_id,
                        api_hash=decrypt_if_encrypted(acc.api_hash),
                        on_message_callback=_telegram_inbound_callback,
                    )
                    logger.info("Auto-started listener for account " + str(acc.id))
                except Exception as e:
                    logger.error("Failed to start listener: " + str(e))
    except Exception as e:
        logger.error("Failed to auto-start listeners: " + str(e))

    yield

    logger.info("Shutting down...")
    for task in [processor_task, token_task, watchdog_task, openline_poller_task, backup_task, health_task]:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    from app.telegram.service import stop_all
    await stop_all()
    outbox.cleanup()
    logger.info("Shutdown complete")


app = FastAPI(title="SAFY Telegram - Bitrix24 Open Lines Connector", lifespan=lifespan)
app.middleware("http")(admin_auth_middleware)


@app.get("/media/{filename:path}")
async def get_media(filename: str, request: Request):
    expires = request.query_params.get("expires")
    token = request.query_params.get("token")
    if not verify_media_token(filename, expires, token):
        raise HTTPException(status_code=403, detail="Invalid media token")
    path = safe_media_path(filename)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Media not found")
    return FileResponse(path)


app.include_router(router)
app.include_router(admin_router)
app.include_router(tg_router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=3000, reload=False)
