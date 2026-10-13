import asyncio
import sys
sys.path.insert(0, '/app')
import os
os.chdir('/app')

from app.database import async_session, OutboxEntry, Message, ChatLink
from sqlalchemy import select, update
from datetime import datetime, timezone


async def main():
    async with async_session() as s:
        # Reset stuck 'processing' entries to 'pending'
        result = await s.execute(
            select(OutboxEntry).where(OutboxEntry.status == 'processing')
        )
        stuck = result.scalars().all()
        for e in stuck:
            e.status = 'pending'
            e.attempts = 0
            e.last_error = None
            e.next_retry_at = datetime.now(timezone.utc)
            print(f"Reset outbox entry {e.id} to pending")
        await s.commit()

        # Delete duplicate ChatLinks (keep the one with bitrix_chat_id if possible)
        links = (await s.execute(
            select(ChatLink).where(
                ChatLink.external_chat_id == '539764128',
                ChatLink.provider == 'tg_6',
            ).order_by(ChatLink.id)
        )).scalars().all()

        print(f"Found {len(links)} ChatLinks for So1oRanker with provider=tg_6:")
        for l in links:
            print(f"  id={l.id} bitrix_chat_id={l.bitrix_chat_id}")

        if len(links) > 1:
            # Keep the one with bitrix_chat_id set, delete others
            keep = next((l for l in links if l.bitrix_chat_id), links[0])
            print(f"Keeping ChatLink id={keep.id}")
            for l in links:
                if l.id != keep.id:
                    # Reassign messages to the keeper
                    await s.execute(
                        update(Message).where(Message.chat_link_id == l.id).values(chat_link_id=keep.id)
                    )
                    await s.delete(l)
                    print(f"Deleted duplicate ChatLink id={l.id}")
            await s.commit()

        # Show failed inbound messages
        msgs = (await s.execute(
            select(Message).where(Message.status == 'failed', Message.direction == 'inbound')
        )).scalars().all()
        print(f"\nFailed inbound messages: {len(msgs)}")
        for m in msgs:
            print(f"  id={m.id} text={repr(m.text[:40])} idempotency={m.idempotency_key}")


asyncio.run(main())
