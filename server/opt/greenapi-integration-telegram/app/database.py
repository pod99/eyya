from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy import Column, String, Text, DateTime, Integer, ForeignKey
from sqlalchemy import text as sa_text
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError
from datetime import datetime, timezone
import os
from app.config import settings

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_DIR = os.path.join(PROJECT_ROOT, "data")
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "app.db")

DATABASE_URL = settings.database_url
for data_prefix in ("sqlite+aiosqlite:///data/", "sqlite+aiosqlite:////data/"):
    if DATABASE_URL.startswith(data_prefix):
        DATABASE_URL = DATABASE_URL.replace(data_prefix, f"sqlite+aiosqlite:///{DB_DIR}/", 1)
        break

connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args["timeout"] = 30

engine = create_async_engine(DATABASE_URL, echo=False, connect_args=connect_args)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def _is_duplicate_column_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "duplicate column" in text or
        "already exists" in text or
        "duplicate_column" in text
    )


class Base(DeclarativeBase):
    pass


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Tenant(Base):
    __tablename__ = "tenants"

    id = Column(Integer, primary_key=True, autoincrement=True)
    bitrix_domain = Column(String(255), unique=True, nullable=False)
    member_id = Column(String(255), nullable=True)
    access_token_encrypted = Column(Text, nullable=False)
    refresh_token_encrypted = Column(Text, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    install_user_id = Column(Integer, nullable=True)
    status = Column(String(50), default="active")
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    connectors = relationship("Connector", back_populates="tenant", cascade="all, delete-orphan")
    channels = relationship("Channel", back_populates="tenant", cascade="all, delete-orphan")
    chat_links = relationship("ChatLink", back_populates="tenant", cascade="all, delete-orphan")
    telegram_accounts = relationship("TelegramAccount", back_populates="tenant", cascade="all, delete-orphan")


class Connector(Base):
    __tablename__ = "connectors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    connector_id = Column(String(100), nullable=False)
    name = Column(String(255), nullable=False)
    status = Column(String(50), default="registered")
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    tenant = relationship("Tenant", back_populates="connectors")


class Channel(Base):
    __tablename__ = "channels"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    provider = Column(String(100), nullable=False)
    name = Column(String(255), nullable=False)
    bitrix_line_id = Column(Integer, nullable=True)
    status = Column(String(50), default="active")
    settings_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    tenant = relationship("Tenant", back_populates="channels")


class ChatLink(Base):
    __tablename__ = "chat_links"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    channel_id = Column(Integer, ForeignKey("channels.id"), nullable=True)
    provider = Column(String(100), nullable=False)
    external_chat_id = Column(String(255), nullable=False)
    external_user_id = Column(String(255), nullable=True)
    external_user_name = Column(String(255), nullable=True)
    bitrix_line_id = Column(Integer, nullable=True)
    bitrix_chat_id = Column(String(255), nullable=True)
    bitrix_dialog_id = Column(String(255), nullable=True)
    crm_entity_type = Column(String(50), nullable=True)
    crm_entity_id = Column(String(255), nullable=True)
    account_id = Column(Integer, ForeignKey("telegram_accounts.id", ondelete="SET NULL"), nullable=True)
    last_message_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    tenant = relationship("Tenant", back_populates="chat_links")
    messages = relationship("Message", back_populates="chat_link", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    channel_id = Column(Integer, ForeignKey("channels.id"), nullable=True)
    chat_link_id = Column(Integer, ForeignKey("chat_links.id"), nullable=True)
    direction = Column(String(20), nullable=False)
    provider_message_id = Column(String(255), nullable=True)
    bitrix_message_id = Column(String(255), nullable=True)
    text = Column(Text, nullable=True)
    status = Column(String(50), default="pending")
    error = Column(Text, nullable=True)
    raw_payload_json = Column(Text, nullable=True)
    idempotency_key = Column(String(500), nullable=True, unique=True)
    account_id = Column(Integer, ForeignKey("telegram_accounts.id", ondelete="SET NULL"), nullable=True)
    chat_id_tg = Column(String(255), nullable=True)
    sender_name = Column(String(255), nullable=True)
    recipient = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    chat_link = relationship("ChatLink", back_populates="messages")


class OutboxEntry(Base):
    __tablename__ = "outbox_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    direction = Column(String(50), nullable=False)
    payload_json = Column(Text, nullable=False)
    max_retries = Column(Integer, default=3)
    attempts = Column(Integer, default=0)
    status = Column(String(50), default="pending")
    last_error = Column(Text, nullable=True)
    next_retry_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class TelegramAccount(Base):
    __tablename__ = "telegram_accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    api_id = Column(Integer, nullable=False)
    api_hash = Column(String(255), nullable=False)
    phone = Column(String(50), nullable=True)
    username = Column(String(255), nullable=True)
    session_string = Column(Text, nullable=True)
    bitrix_line_id = Column(Integer, nullable=True)
    bitrix_connector_id = Column(String(100), nullable=True)
    state = Column(String(50), default="pending")
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    tenant = relationship("Tenant", back_populates="telegram_accounts")


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    actor = Column(String(255), nullable=True)
    ip = Column(String(100), nullable=True)
    method = Column(String(20), nullable=False)
    path = Column(String(500), nullable=False)
    action = Column(String(255), nullable=True)
    status_code = Column(Integer, nullable=True)
    details_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow)


class QuickReplyTemplate(Base):
    __tablename__ = "quick_reply_templates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    title = Column(String(120), nullable=False)
    text = Column(Text, nullable=False)
    sort_order = Column(Integer, default=0)
    active = Column(Integer, default=1)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if DATABASE_URL.startswith("postgresql"):
            postgres_migrations = [
                ("messages", "account_id", "INTEGER"),
                ("messages", "chat_id_tg", "VARCHAR(255)"),
                ("messages", "sender_name", "VARCHAR(255)"),
                ("messages", "recipient", "VARCHAR(255)"),
                ("chat_links", "crm_entity_type", "VARCHAR(50)"),
                ("chat_links", "crm_entity_id", "VARCHAR(255)"),
                ("chat_links", "account_id", "INTEGER"),
            ]
            for table, col, col_type in postgres_migrations:
                await conn.execute(sa_text(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {col_type}"
                ))
            await conn.execute(sa_text(
                "UPDATE chat_links SET account_id = NULL "
                "WHERE account_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM telegram_accounts WHERE telegram_accounts.id = chat_links.account_id)"
            ))
            await conn.execute(sa_text(
                "UPDATE messages SET account_id = NULL "
                "WHERE account_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM telegram_accounts WHERE telegram_accounts.id = messages.account_id)"
            ))
            await conn.execute(sa_text("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint WHERE conname = 'chat_links_account_id_fkey'
                    ) THEN
                        ALTER TABLE chat_links
                        ADD CONSTRAINT chat_links_account_id_fkey
                        FOREIGN KEY (account_id) REFERENCES telegram_accounts(id) ON DELETE SET NULL;
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint WHERE conname = 'messages_account_id_fkey'
                    ) THEN
                        ALTER TABLE messages
                        ADD CONSTRAINT messages_account_id_fkey
                        FOREIGN KEY (account_id) REFERENCES telegram_accounts(id) ON DELETE SET NULL;
                    END IF;
                END $$;
            """))
            await conn.execute(sa_text(
                "CREATE INDEX IF NOT EXISTS idx_messages_text_fts "
                "ON messages USING GIN (to_tsvector('simple', coalesce(text, '')))"
            ))
            await conn.execute(sa_text(
                "CREATE INDEX IF NOT EXISTS idx_messages_created_at "
                "ON messages (created_at DESC)"
            ))
            await conn.execute(sa_text(
                "CREATE INDEX IF NOT EXISTS idx_chat_links_tenant_provider_account_external "
                "ON chat_links (tenant_id, provider, account_id, external_chat_id)"
            ))
            await conn.execute(sa_text(
                "CREATE INDEX IF NOT EXISTS idx_messages_tenant_account_recipient "
                "ON messages (tenant_id, account_id, recipient)"
            ))
            await conn.execute(sa_text(
                "CREATE INDEX IF NOT EXISTS idx_messages_chat_link_account "
                "ON messages (chat_link_id, account_id)"
            ))
        if not DATABASE_URL.startswith("sqlite"):
            return
        # Safe migration: add new columns if they don't exist yet
        migrations = [
            ("messages", "account_id", "INTEGER"),
            ("messages", "chat_id_tg", "TEXT"),
            ("messages", "sender_name", "TEXT"),
            ("messages", "recipient", "TEXT"),
            ("chat_links", "crm_entity_type", "TEXT"),
            ("chat_links", "crm_entity_id", "TEXT"),
            ("chat_links", "account_id", "INTEGER"),
        ]
        for table, col, col_type in migrations:
            try:
                await conn.execute(sa_text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
            except SQLAlchemyError as exc:
                if not _is_duplicate_column_error(exc):
                    raise


async def get_db():
    async with async_session() as session:
        yield session
