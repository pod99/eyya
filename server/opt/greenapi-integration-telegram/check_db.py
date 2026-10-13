import asyncio
import sys
sys.path.insert(0, '/app')
import os
os.chdir('/app')

from app.database import async_session, Message, ChatLink, OutboxEntry
from sqlalchemy import select, desc


async def main():
    async with async_session() as s:
        msgs = (await s.execute(select(Message).order_by(desc(Message.id)).limit(10))).scalars().all()
        print('=== Messages ===')
        for m in msgs:
            text_preview = repr(m.text[:60]) if m.text else None
            print(f'id={m.id} dir={m.direction} status={m.status} text={text_preview} error={m.error}')

        chats = (await s.execute(select(ChatLink))).scalars().all()
        print('=== ChatLinks ===')
        for c in chats:
            print(f'id={c.id} ext_chat={c.external_chat_id} bx_chat={c.bitrix_chat_id} account={c.account_id}')

        outbox = (await s.execute(select(OutboxEntry).order_by(desc(OutboxEntry.id)).limit(10))).scalars().all()
        print('=== OutboxEntries (latest 10) ===')
        for o in outbox:
            print(f'id={o.id} dir={o.direction} status={o.status} attempts={o.attempts} err={o.last_error}')


asyncio.run(main())
