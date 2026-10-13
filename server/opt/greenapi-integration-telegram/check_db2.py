import asyncio
import sys
sys.path.insert(0, '/app')
import os
os.chdir('/app')

from app.database import async_session, Message, ChatLink, OutboxEntry
from sqlalchemy import select, desc, inspect


async def main():
    async with async_session() as s:
        # Check ChatLink columns
        chats = (await s.execute(select(ChatLink))).scalars().all()
        print('=== ChatLinks ===')
        if chats:
            first = chats[0]
            print(f'  columns: {[c.key for c in inspect(first).mapper.column_attrs]}')
            for c in chats:
                print(f'  {c.__dict__}')
        else:
            print('  (none)')

        # Recent failed messages with details
        msgs = (await s.execute(
            select(Message).where(Message.status == 'failed').order_by(desc(Message.id)).limit(5)
        )).scalars().all()
        print('=== Failed Messages ===')
        for m in msgs:
            print(f'  id={m.id} dir={m.direction} idempotency={m.idempotency_key} text={repr(m.text[:60] if m.text else "")} error={m.error}')

        # Outbox
        outbox = (await s.execute(select(OutboxEntry).order_by(desc(OutboxEntry.id)).limit(5))).scalars().all()
        print('=== OutboxEntries ===')
        if outbox:
            first = outbox[0]
            print(f'  columns: {[c.key for c in inspect(first).mapper.column_attrs]}')
        for o in outbox:
            print(f'  id={o.id} status={o.status} attempts={o.attempts} err={o.last_error} payload={o.payload_json[:80]}')


asyncio.run(main())
