import abc
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class BaseProviderAdapter(abc.ABC):
    @abc.abstractmethod
    async def send_message(
        self,
        channel_id: str,
        external_chat_id: str,
        text: str,
        attachments: Optional[list] = None,
    ) -> dict:
        pass

    @abc.abstractmethod
    async def health_check(self) -> bool:
        pass
