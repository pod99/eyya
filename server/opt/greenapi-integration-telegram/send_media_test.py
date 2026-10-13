import asyncio
import os
import sys
from io import BytesIO

sys.path.insert(0, '/app')
os.chdir('/app')

from telethon.sessions import StringSession


async def main():
    from app.config import settings
    from app.database import async_session, TelegramAccount
    from sqlalchemy import select
    from telethon import TelegramClient
    from telethon.network.connection.tcpabridged import ConnectionTcpAbridged
    from telethon.tl.types import DocumentAttributeFilename
    import socks

    async with async_session() as session:
        result = await session.execute(select(TelegramAccount).where(TelegramAccount.id == 6))
        account = result.scalar_one_or_none()

    if not account:
        print("ERROR: Account 6 not found in DB")
        return

    print(f"Account: @{account.username}, state={account.state}")

    # Get session string (stored encrypted or plain depending on model version)
    session_str = None
    if hasattr(account, 'session_string_encrypted') and account.session_string_encrypted:
        from app.crypto import decrypt_token
        session_str = decrypt_token(account.session_string_encrypted)
    elif account.session_string:
        # May already be a plain StringSession value
        try:
            from app.crypto import decrypt_token
            session_str = decrypt_token(account.session_string)
        except Exception:
            session_str = account.session_string

    if not session_str:
        print("ERROR: No session string found")
        return

    # API credentials are per-account in DB, not global settings
    api_id = account.api_id or settings.telegram_api_id
    api_hash = account.api_hash or settings.telegram_api_hash
    print(f"Using api_id={api_id}, api_hash={'SET' if api_hash else 'EMPTY'}")

    proxy = (socks.SOCKS5, settings.proxy_host, settings.proxy_port)
    client = TelegramClient(
        StringSession(session_str),
        api_id,
        api_hash,
        proxy=proxy,
        connection=ConnectionTcpAbridged,
    )
    await client.connect()
    print("Connected to Telegram")

    recipient = '@So1oRanker'

    # --- Send photo ---
    print('Sending photo...')
    try:
        from PIL import Image
        img = Image.new('RGB', (640, 480), color=(73, 109, 137))
        from PIL import ImageDraw
        draw = ImageDraw.Draw(img)
        draw.rectangle([50, 50, 590, 430], fill=(100, 150, 200))
        draw.text((200, 220), "VanTech Omni", fill=(255, 255, 255))
        buf = BytesIO()
        img.save(buf, format='JPEG', quality=85)
        buf.seek(0)
        buf.name = 'test_photo.jpg'
        await client.send_file(recipient, buf, caption='Test photo from VanTech Omni integration')
        print('Photo sent!')
    except Exception as e:
        print(f"Photo send error: {e}")

    await asyncio.sleep(2)

    # --- Send file (PDF) ---
    print('Sending PDF file...')
    try:
        pdf_content = b'%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>\nendobj\nxref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \ntrailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n190\n%%EOF\n'
        file_buf = BytesIO(pdf_content)
        file_buf.name = 'test_document.pdf'
        attrs = [DocumentAttributeFilename('test_document.pdf')]
        await client.send_file(
            recipient,
            file_buf,
            caption='Test document from VanTech Omni integration',
            attributes=attrs,
            force_document=True,
        )
        print('PDF file sent!')
    except Exception as e:
        print(f"File send error: {e}")

    await client.disconnect()
    print('Done!')


asyncio.run(main())
