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

    async with httpx.AsyncClient(timeout=15) as client:

        # Check tg_6 connector status
        print("=== imconnector.status (tg_6) ===")
        r = await client.post(f"{base_url}/imconnector.status",
                              params={"auth": token},
                              json={"CONNECTOR": "tg_6"})
        print(f"  {r.json()}")

        # Check what events are bound for tg_6 specifically
        print("\n=== imconnector.connector.data.get (tg_6) ===")
        r = await client.post(f"{base_url}/imconnector.connector.data.get",
                              params={"auth": token},
                              json={"CONNECTOR": "tg_6"})
        print(f"  {r.json()}")

        # Check which open line 30 uses
        print("\n=== imopenlines.config.get (line 30) ===")
        r = await client.post(f"{base_url}/imopenlines.config.get",
                              params={"auth": token},
                              json={"CONFIG_ID": 30})
        data = r.json()
        if "result" in data:
            cfg = data["result"]
            print(f"  ID={cfg.get('ID')} NAME={cfg.get('LINE_NAME')} ACTIVE={cfg.get('ACTIVE')}")
        else:
            print(f"  {data}")

        # Test accessibility of our webhook from outside
        print("\n=== Testing webhook URL accessibility ===")
        for url in [
            "REPLACE_WITH_PRIVATE_WEBHOOK_URLbitrix/events",
            "http://n8n.eyya.skin:3000/bitrix/events",
            "http://n8n.eyya.skin:3000/health",
        ]:
            try:
                r = await client.get(url.replace("/bitrix/events", "/health"), timeout=5)
                print(f"  {url} → status {r.status_code}")
            except Exception as e:
                print(f"  {url} → ERROR: {type(e).__name__}: {e}")

        # Re-register the event binding for tg_6
        print("\n=== Re-registering ONIMCONNECTORMESSAGEADD event ===")
        app_url = "http://n8n.eyya.skin:3000"
        r = await client.post(f"{base_url}/event.unbind",
                              params={"auth": token},
                              json={
                                  "event": "ONIMCONNECTORMESSAGEADD",
                                  "handler": "REPLACE_WITH_PRIVATE_WEBHOOK_URLbitrix/events",
                              })
        print(f"  Unbind HTTPS: {r.json()}")

        r = await client.post(f"{base_url}/event.bind",
                              params={"auth": token},
                              json={
                                  "event": "ONIMCONNECTORMESSAGEADD",
                                  "handler": f"{app_url}/bitrix/events",
                              })
        print(f"  Bind HTTP:3000: {r.json()}")

        # Verify current state
        print("\n=== event.get (after fix) ===")
        r = await client.get(f"{base_url}/event.get", params={"auth": token})
        data = r.json()
        if "result" in data:
            for ev in data["result"]:
                print(f"  {ev}")
        else:
            print(f"  {data}")

        # Activate connector properly for tg_6
        print("\n=== Activating tg_6 connector ===")
        r = await client.post(f"{base_url}/imconnector.activate",
                              params={"auth": token},
                              json={
                                  "CONNECTOR": "tg_6",
                                  "LINE": 30,
                              })
        print(f"  {r.json()}")

        print("\n=== imconnector.status (tg_6) after activate ===")
        r = await client.post(f"{base_url}/imconnector.status",
                              params={"auth": token},
                              json={"CONNECTOR": "tg_6"})
        print(f"  {r.json()}")


asyncio.run(main())
