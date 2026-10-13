import asyncio
import sys
sys.path.insert(0, '/app')
import os
os.chdir('/app')

from app.database import async_session, Tenant
from app.crypto import decrypt_token
from sqlalchemy import select
import httpx


async def main():
    async with async_session() as s:
        tenant = (await s.execute(select(Tenant))).scalar_one()

    domain = tenant.bitrix_domain
    token = decrypt_token(tenant.access_token_encrypted)
    base_url = f"https://{domain}/rest"
    app_url = "http://n8n.eyya.skin:3000"
    connector_id = "tg_6"
    line_id = 30

    async with httpx.AsyncClient(timeout=15) as client:

        # Try imconnector.connector.data.set to mark as configured
        print("1. Setting connector data...")
        r = await client.post(f"{base_url}/imconnector.connector.data.set",
                              params={"auth": token},
                              json={
                                  "CONNECTOR": connector_id,
                                  "LINE": line_id,
                                  "DATA": {
                                      "TOKEN": "tg_account_6",
                                      "USER_DATA": {
                                          "NAME": "Telegram @eyyabeauty",
                                          "PICTURE": "",
                                          "URL": app_url,
                                      },
                                  },
                              })
        print(f"   {r.json()}")

        # Check status
        print("\n2. Status after data.set:")
        r = await client.post(f"{base_url}/imconnector.status",
                              params={"auth": token},
                              json={"CONNECTOR": connector_id})
        status = r.json().get("result", {})
        print(f"   CONFIGURED={status.get('CONFIGURED')} STATUS={status.get('STATUS')} LINE={status.get('LINE')}")

        # Try imopenlines.session.list to see active sessions for our connector
        print("\n3. Getting open line sessions for chat_id 28534:")
        r = await client.post(f"{base_url}/im.dialog.get",
                              params={"auth": token},
                              json={"DIALOG_ID": f"chat28534"})
        print(f"   {r.json()}")

        # Check if session exists for this connector in the open line
        print("\n4. imopenlines.session.list for line 30:")
        r = await client.post(f"{base_url}/imopenlines.session.list",
                              params={"auth": token},
                              json={"FILTER": {"CHAT_ID": 28534}})
        print(f"   {r.json()}")

        # Try manual event test - send a test event to our webhook
        print("\n5. Testing webhook manually:")
        r = await client.post(f"{app_url}/bitrix/events",
                              json={
                                  "auth": {"domain": domain},
                                  "event": "ONIMCONNECTORMESSAGEADD",
                                  "data": {
                                      "CONNECTOR": connector_id,
                                      "MESSAGES": [{
                                          "im": {"chat_id": "28534", "message_id": "manual_test_1"},
                                          "message": {"id": ["manual_test_1"], "text": "Ручной тест ответа оператора", "files": []},
                                          "chat": {"id": "28534"},
                                      }]
                                  }
                              })
        print(f"   webhook result: {r.json()}")


asyncio.run(main())
