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

    print(f"Domain: {domain}")
    print(f"Token: {token[:20]}...")

    async with httpx.AsyncClient(timeout=15) as client:
        # Check registered event bindings
        print("\n=== event.get (registered webhooks) ===")
        r = await client.get(f"{base_url}/event.get", params={"auth": token})
        data = r.json()
        if "result" in data:
            for ev in data["result"]:
                print(f"  {ev}")
        else:
            print(f"  {data}")

        # Check imconnector status
        print("\n=== imconnector.list ===")
        r = await client.post(f"{base_url}/imconnector.list", params={"auth": token})
        data = r.json()
        print(f"  {data}")

        # Check connector status specifically
        print("\n=== imconnector.status (vantech_omni) ===")
        r = await client.post(f"{base_url}/imconnector.status",
                              params={"auth": token},
                              json={"CONNECTOR": "vantech_omni"})
        data = r.json()
        print(f"  {data}")

        # Check what URL the events are sent to
        print("\n=== app.info ===")
        r = await client.get(f"{base_url}/app.info", params={"auth": token})
        data = r.json()
        if "result" in data:
            for k, v in data["result"].items():
                print(f"  {k}: {v}")
        else:
            print(f"  {data}")


asyncio.run(main())
