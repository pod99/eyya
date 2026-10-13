import logging
import httpx
from typing import Optional
from app.providers.base import BaseProviderAdapter

logger = logging.getLogger(__name__)


class HttpProviderAdapter(BaseProviderAdapter):
    def __init__(self, webhook_url: str, headers: dict = None, timeout: int = 30):
        self.webhook_url = webhook_url
        self.headers = headers or {"Content-Type": "application/json"}
        self.timeout = timeout

    async def send_message(
        self,
        channel_id: str,
        external_chat_id: str,
        text: str,
        attachments: Optional[list] = None,
    ) -> dict:
        payload = {
            "channel_id": channel_id,
            "external_chat_id": external_chat_id,
            "text": text,
        }
        if attachments:
            payload["attachments"] = attachments

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    self.webhook_url,
                    json=payload,
                    headers=self.headers,
                    timeout=self.timeout,
                )
                result = resp.json() if resp.status_code < 500 else {"error": resp.text}
                logger.info(f"HTTP provider sent to {self.webhook_url}: {resp.status_code}")
                return {"success": resp.status_code < 400, "data": result}
        except Exception as e:
            logger.error(f"HTTP provider error: {e}")
            return {"success": False, "error": str(e)}

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(self.webhook_url, timeout=5)
                return resp.status_code < 500
        except Exception:
            return False
