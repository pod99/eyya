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

    print(f"Domain: {domain}, connector: {connector_id}, line: {line_id}")

    async with httpx.AsyncClient(timeout=15) as client:

        # 1. Register connector (idempotent)
        print("\n1. Registering connector...")
        svg = "data:image/svg+xml;charset=US-ASCII,%3Csvg%20xmlns%3D%22http%3A//www.w3.org/2000/svg%22%20viewBox%3D%220%200%2070%2071%22%3E%3Cpath%20fill%3D%22%230088cc%22%20d%3D%22M34.7%2C64c-11.6%2C0-22-7.1-26.3-17.8C4%2C35.4%2C6.4%2C23%2C14.5%2C14.7c8.1-8.2%2C20.4-10.7%2C31-6.2c12.5%2C5.4%2C19.6%2C18.8%2C17%2C32.2C60%2C54%2C48.3%2C63.8%2C34.7%2C64z%22/%3E%3C/svg%3E"
        r = await client.post(f"{base_url}/imconnector.register", params={"auth": token}, json={
            "ID": connector_id,
            "NAME": "Telegram @eyyabeauty",
            "ICON": {"DATA_IMAGE": svg, "COLOR": "#0088cc"},
            "PLACEMENT_HANDLER": f"{app_url}/bitrix/events",
        })
        print(f"   Result: {r.json()}")

        # 2. Activate connector for line 30 — use True (bool) not 1
        print("\n2. Activating connector with ACTIVE=True...")
        r = await client.post(f"{base_url}/imconnector.activate", params={"auth": token}, json={
            "CONNECTOR": connector_id,
            "LINE": line_id,
            "ACTIVE": True,
        })
        print(f"   Result: {r.json()}")

        # 3. Try with "Y" if True didn't work
        status_r = await client.post(f"{base_url}/imconnector.status",
                                     params={"auth": token},
                                     json={"CONNECTOR": connector_id})
        status = status_r.json().get("result", {})
        print(f"\n3. Status after activate(True): CONFIGURED={status.get('CONFIGURED')} STATUS={status.get('STATUS')} LINE={status.get('LINE')}")

        if not status.get("CONFIGURED"):
            print("   Trying ACTIVE='Y'...")
            r = await client.post(f"{base_url}/imconnector.activate", params={"auth": token}, json={
                "CONNECTOR": connector_id,
                "LINE": line_id,
                "ACTIVE": "Y",
            })
            print(f"   Result: {r.json()}")

            status_r = await client.post(f"{base_url}/imconnector.status",
                                         params={"auth": token},
                                         json={"CONNECTOR": connector_id})
            status = status_r.json().get("result", {})
            print(f"   Status after activate('Y'): CONFIGURED={status.get('CONFIGURED')} STATUS={status.get('STATUS')} LINE={status.get('LINE')}")

        # 4. Bind event handler
        print("\n4. Binding ONIMCONNECTORMESSAGEADD...")
        r = await client.post(f"{base_url}/event.bind", params={"auth": token}, json={
            "event": "ONIMCONNECTORMESSAGEADD",
            "handler": f"{app_url}/bitrix/events",
        })
        print(f"   Result: {r.json()}")

        # 5. Final status
        print("\n5. Final event.get:")
        r = await client.get(f"{base_url}/event.get", params={"auth": token})
        data = r.json()
        if "result" in data:
            for ev in data["result"]:
                print(f"   {ev}")

        print("\n5. Final imconnector.status (tg_6):")
        r = await client.post(f"{base_url}/imconnector.status",
                              params={"auth": token},
                              json={"CONNECTOR": connector_id})
        print(f"   {r.json()}")


asyncio.run(main())
