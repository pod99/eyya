from pydantic_settings import BaseSettings
from pydantic import field_validator


class Settings(BaseSettings):
    database_url: str = ""
    bitrix24_client_id: str = ""
    bitrix24_client_secret: str = ""
    app_url: str = "http://localhost:3000"
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    proxy_host: str = "127.0.0.1"
    proxy_port: int = 10808
    encryption_key: str = ""
    connector_id: str = "safy_telegram"
    connector_name: str = "SAFY Telegram"
    primary_connector_id: str = "tg_6"
    primary_bitrix_line_id: int = 30
    create_lead_by_default: bool = True
    admin_username: str = ""
    admin_password: str = ""
    trusted_proxy_ips: str = "127.0.0.1,::1"
    backup_retention_days: int = 14
    min_backup_interval_seconds: int = 6 * 3600
    health_check_interval_seconds: int = 300
    health_alert_webhook_url: str = ""
    health_alert_telegram_bot_token: str = ""
    health_alert_telegram_chat_id: str = ""
    bitrix_webhook_hmac_required: bool = True
    bitrix_webhook_token: str = ""
    inbound_webhook_token: str = ""
    allowed_bitrix_domain_suffixes: str = "bitrix24.ru,bitrix24.com,bitrix24.eu,bitrix24.tech"
    max_public_request_bytes: int = 2 * 1024 * 1024
    max_widget_request_bytes: int = 180 * 1024 * 1024
    media_url_secret: str = ""
    media_url_ttl_seconds: int = 86400
    media_retention_seconds: int = 90 * 24 * 3600
    media_cleanup_interval_seconds: int = 24 * 3600
    max_telegram_media_bytes: int = 1024 * 1024 * 1024
    pending_login_ttl_seconds: int = 600
    cache_ttl_seconds: int = 3600
    cache_max_size: int = 1000
    real_operator_name: str = "Умей Смеяться"
    unknown_bitrix_operator_name: str = "Менеджер службы заботы Виктория"
    service_operator_names: str = "SAFY Test Operator,Bitrix widget,SAFY Telegram,eyyabeauty,Telegram @eyyabeauty"
    fallback_operator_names: str = "Bitrix24 / Open Line,Bitrix operator"
    test_text_prefixes: str = "safy synthetic,safy live,safy test,poller self-test"

    @field_validator("telegram_api_id", mode="before")
    @classmethod
    def parse_api_id(cls, v):
        if isinstance(v, str):
            try:
                return int(v)
            except ValueError:
                return 0
        return v

    @field_validator("database_url")
    @classmethod
    def require_database_url(cls, v):
        if not str(v or "").strip():
            raise ValueError("DATABASE_URL is required")
        return v

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
