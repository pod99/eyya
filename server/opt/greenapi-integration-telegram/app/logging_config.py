import json
import logging
import sys
from datetime import datetime, timezone


class StructuredFormatter(logging.Formatter):
    def format(self, record):
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if hasattr(record, "extra_data"):
            log_entry["data"] = record.extra_data

        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = self.formatException(record.exc_info)

        sensitive_keys = {"access_token", "refresh_token", "bot_token", "secret", "password", "api_key", "api_token"}
        log_str = json.dumps(log_entry, default=str)
        for key in sensitive_keys:
            if key in log_str.lower():
                import re
                log_str = re.sub(
                    rf'("{key}"\s*:\s*")[^"]+(")',
                    r'\1***REDACTED***\2',
                    log_str,
                    flags=re.IGNORECASE,
                )
                break

        return log_str


def setup_logging(level: str = "INFO"):
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    for handler in root.handlers[:]:
        root.removeHandler(handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(StructuredFormatter())
    root.addHandler(console)

    for noisy in ["httpx", "httpcore", "urllib3", "asyncio"]:
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logger = logging.getLogger(__name__)
    logger.info("Logging initialized", extra={"extra_data": {"level": level}})
    return logger


def log_api_call(method: str, domain: str, status: str, duration_ms: float = None,
                 error: str = None, extra: dict = None):
    logger = logging.getLogger("api_calls")
    data = {"method": method, "domain": domain, "status": status}
    if duration_ms is not None:
        data["duration_ms"] = round(duration_ms, 2)
    if error:
        data["error"] = error
    if extra:
        data.update(extra)
    logger.info(f"API call: {method} -> {status}", extra={"extra_data": data})


def log_message_event(direction: str, provider: str, channel_id: str,
                      message_id: str = None, status: str = None, error: str = None):
    logger = logging.getLogger("messages")
    data = {"direction": direction, "provider": provider, "channel_id": channel_id}
    if message_id:
        data["message_id"] = message_id
    if status:
        data["status"] = status
    if error:
        data["error"] = error
    logger.info(f"Message: {direction} via {provider}", extra={"extra_data": data})
