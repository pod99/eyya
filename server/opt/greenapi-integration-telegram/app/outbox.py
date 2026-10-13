import json
import logging
from datetime import timedelta
from typing import Optional

logger = logging.getLogger(__name__)


class OutboxMessage:
    def __init__(self, direction: str, payload: dict, target_url: str = None,
                 max_retries: int = 3, provider: str = None, channel_id: int = None):
        self.direction = direction
        self.payload = payload
        self.target_url = target_url
        self.max_retries = max_retries
        self.provider = provider
        self.channel_id = channel_id


class OutboxQueue:
    def __init__(self):
        self._handlers: dict[str, callable] = {}

    def register_handler(self, direction: str, handler: callable):
        self._handlers[direction] = handler

    async def enqueue(self, message: OutboxMessage) -> int:
        from app.database import async_session, OutboxEntry
        from app.database import utcnow
        async with async_session() as session:
            entry = OutboxEntry(
                direction=message.direction,
                payload_json=json.dumps(message.payload),
                max_retries=message.max_retries,
                status="pending",
                next_retry_at=utcnow(),
            )
            session.add(entry)
            await session.commit()
            await session.refresh(entry)
            logger.info(f"Outbox: enqueued entry {entry.id} ({message.direction})")
            return entry.id

    async def process_next(self) -> bool:
        from app.database import async_session, OutboxEntry
        from app.database import utcnow
        from sqlalchemy import select, update
        async with async_session() as session:
            now = utcnow()
            dialect = session.get_bind().dialect.name
            if dialect == "postgresql":
                candidate_id = (
                    select(OutboxEntry.id)
                    .where(OutboxEntry.status == "pending", OutboxEntry.next_retry_at <= now)
                    .order_by(OutboxEntry.next_retry_at)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                    .scalar_subquery()
                )
                result = await session.execute(
                    update(OutboxEntry)
                    .where(OutboxEntry.id == candidate_id)
                    .values(status="processing", updated_at=now)
                    .returning(
                        OutboxEntry.id,
                        OutboxEntry.direction,
                        OutboxEntry.payload_json,
                        OutboxEntry.attempts,
                        OutboxEntry.max_retries,
                    )
                )
                row = result.first()
                if not row:
                    return False
                await session.commit()
                entry_id, direction, payload_json, attempts, max_retries = row
            else:
                result = await session.execute(
                    select(OutboxEntry)
                    .where(OutboxEntry.status == "pending", OutboxEntry.next_retry_at <= now)
                    .order_by(OutboxEntry.next_retry_at)
                    .limit(1)
                )
                entry = result.scalar_one_or_none()
                if not entry:
                    return False
                entry_id = entry.id
                direction = entry.direction
                payload_json = entry.payload_json
                attempts = entry.attempts
                max_retries = entry.max_retries

                await session.execute(
                    update(OutboxEntry).where(
                        OutboxEntry.id == entry_id,
                        OutboxEntry.status == "pending",
                    ).values(
                        status="processing",
                        updated_at=now,
                    )
                )
                await session.commit()

        await self._process_entry(entry_id, direction, payload_json, attempts, max_retries)
        return True

    async def recover_stale_processing(self, older_than_seconds: int = 300) -> int:
        from app.database import async_session, OutboxEntry
        from app.database import utcnow
        from sqlalchemy import select, update

        cutoff = utcnow() - timedelta(seconds=max(60, int(older_than_seconds or 300)))
        async with async_session() as session:
            ids = [
                entry_id for (entry_id,) in (await session.execute(
                    select(OutboxEntry.id)
                    .where(OutboxEntry.status == "processing", OutboxEntry.updated_at < cutoff)
                )).all()
            ]
            if not ids:
                return 0
            await session.execute(
                update(OutboxEntry)
                .where(OutboxEntry.id.in_(ids))
                .values(
                    status="pending",
                    last_error="Recovered stale processing entry after worker restart",
                    next_retry_at=utcnow(),
                    updated_at=utcnow(),
                )
            )
            await session.commit()
        logger.warning("Outbox: recovered stale processing entries: %s", ids)
        return len(ids)

    async def _process_entry(self, entry_id: int, direction: str, payload_json: str,
                              attempts: int, max_retries: int):
        from app.database import async_session, OutboxEntry
        from app.database import utcnow
        from sqlalchemy import update

        handler = self._handlers.get(direction)
        if not handler:
            async with async_session() as session:
                await session.execute(
                    update(OutboxEntry).where(OutboxEntry.id == entry_id).values(
                        status="failed",
                        last_error=f"No handler for direction: {direction}",
                        updated_at=utcnow(),
                    )
                )
                await session.commit()
            return

        try:
            payload = json.loads(payload_json)
            new_attempts = attempts + 1
            result = await handler(payload)

            async with async_session() as session:
                if result.get("success"):
                    await session.execute(
                        update(OutboxEntry).where(OutboxEntry.id == entry_id).values(
                            status="sent",
                            attempts=new_attempts,
                            updated_at=utcnow(),
                        )
                    )
                    logger.info(f"Outbox: entry {entry_id} sent OK")
                else:
                    raise Exception(result.get("error", "Unknown error"))
                await session.commit()

        except Exception as e:
            new_attempts = attempts + 1
            logger.error(f"Outbox: entry {entry_id} failed (attempt {new_attempts}): {e}")
            async with async_session() as session:
                if new_attempts >= max_retries:
                    await session.execute(
                        update(OutboxEntry).where(OutboxEntry.id == entry_id).values(
                            status="failed",
                            attempts=new_attempts,
                            last_error=str(e),
                            updated_at=utcnow(),
                        )
                    )
                    logger.error(f"Outbox: entry {entry_id} permanently failed after {new_attempts} attempts")
                else:
                    backoff = self._calculate_backoff(new_attempts, str(e))
                    next_retry = utcnow() + timedelta(seconds=backoff)
                    await session.execute(
                        update(OutboxEntry).where(OutboxEntry.id == entry_id).values(
                            status="pending", attempts=new_attempts,
                            last_error=str(e), next_retry_at=next_retry,
                            updated_at=utcnow(),
                        )
                    )
                    logger.info(f"Outbox: entry {entry_id} will retry in {backoff:.1f}s")
                await session.commit()

    def _calculate_backoff(self, attempt: int, error: str) -> float:
        if "QUERY_LIMIT_EXCEEDED" in error or "429" in error:
            base = 10
        elif "OPERATION_TIME_LIMIT" in error:
            base = 60
        elif "503" in error or "500" in error:
            base = 5
        else:
            base = 2
        return min(base * (2 ** (attempt - 1)), 300)

    async def get_stats(self) -> dict:
        from app.database import async_session, OutboxEntry
        from sqlalchemy import select, func
        async with async_session() as session:
            result = await session.execute(
                select(OutboxEntry.status, func.count(OutboxEntry.id)).group_by(OutboxEntry.status)
            )
            rows = result.fetchall()
        stats = {"pending": 0, "processing": 0, "sent": 0, "failed": 0, "total": 0}
        for status, count in rows:
            stats[status] = count
            stats["total"] += count
        return stats

    async def retry_entry(self, entry_id: int) -> bool:
        from app.database import async_session, OutboxEntry
        from app.database import utcnow
        from sqlalchemy import select, update
        async with async_session() as session:
            result = await session.execute(select(OutboxEntry).where(OutboxEntry.id == entry_id))
            entry = result.scalar_one_or_none()
            if not entry or entry.status != "failed":
                return False
            await session.execute(
                update(OutboxEntry).where(OutboxEntry.id == entry_id).values(
                    status="pending", attempts=0, last_error=None,
                    next_retry_at=utcnow(),
                    updated_at=utcnow(),
                )
            )
            await session.commit()
        return True

    def cleanup(self, max_age_seconds: int = 3600):
        pass  # Data is persistent in DB; no in-memory cleanup needed


outbox = OutboxQueue()
