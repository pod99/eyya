import asyncio
import sys
sys.path.insert(0, '/app')
import os
os.chdir('/app')
import json

from app.database import async_session, OutboxEntry, Message, ChatLink
from sqlalchemy import select, delete


async def main():
    async with async_session() as s:
        # Show full payloads of outbox entries
        entries = (await s.execute(select(OutboxEntry).order_by(OutboxEntry.id))).scalars().all()
        print("=== OutboxEntries (full payload) ===")
        for e in entries:
            payload = json.loads(e.payload_json)
            print(f"  id={e.id} status={e.status} account_id={payload.get('account_id')} ext_chat={payload.get('external_chat_id')}")

        # Delete test artifact entries (with external_chat_id=111222333 which is not real)
        test_entries = [e for e in entries if json.loads(e.payload_json).get('external_chat_id') == '111222333']
        for e in test_entries:
            await s.delete(e)
            print(f"  Deleted test outbox entry id={e.id}")
        await s.commit()

        # Also clean up old ChatLinks with no bitrix_chat_id and wrong provider (old test entries)
        old_links = (await s.execute(
            select(ChatLink).where(
                ChatLink.provider.in_(['telegram']),
                ChatLink.bitrix_chat_id == None,
            )
        )).scalars().all()
        print(f"\n=== Old 'telegram' provider ChatLinks without bitrix_chat_id: {len(old_links)} ===")
        for l in old_links:
            print(f"  id={l.id} ext={l.external_chat_id} name={l.external_user_name}")

        print("\nDone.")


asyncio.run(main())
