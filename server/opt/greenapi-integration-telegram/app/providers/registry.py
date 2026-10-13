import json
import logging
from typing import Optional
from app.providers.base import BaseProviderAdapter
from app.providers.http_provider import HttpProviderAdapter

logger = logging.getLogger(__name__)

_registry: dict[str, BaseProviderAdapter] = {}


def register_provider(channel_key: str, adapter: BaseProviderAdapter):
    _registry[channel_key] = adapter
    logger.info(f"Provider registered: {channel_key}")


def get_provider(channel_key: str) -> Optional[BaseProviderAdapter]:
    return _registry.get(channel_key)


def load_providers_from_settings(settings_json: str, channel_key: str):
    try:
        cfg = json.loads(settings_json) if isinstance(settings_json, str) else settings_json
        ptype = cfg.get("type", "http")
        if ptype == "http":
            adapter = HttpProviderAdapter(
                webhook_url=cfg["webhook_url"],
                headers=cfg.get("headers", {}),
                timeout=cfg.get("timeout", 30),
            )
            register_provider(channel_key, adapter)
        else:
            logger.warning(f"Unknown provider type: {ptype}")
    except Exception as e:
        logger.error(f"Failed to load provider for {channel_key}: {e}")
