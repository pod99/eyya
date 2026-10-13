"""
Shared fixtures for all tests.
Uses an isolated PostgreSQL schema when DATABASE_URL points to PostgreSQL,
otherwise falls back to in-memory SQLite for local development.
"""
import asyncio
import os
import uuid
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

# ──────────────────────────────────────────────────────────────────────
# pytest-asyncio: use asyncio mode for all tests in this package
# ──────────────────────────────────────────────────────────────────────
pytest_plugins = ("pytest_asyncio",)

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")


async def _postgres_test_database_url(database_url: str) -> str:
    if os.environ.get("TEST_DATABASE_URL"):
        return database_url
    url = make_url(database_url)
    current_db = url.database or "postgres"
    test_db = current_db if current_db.endswith("_test") else f"{current_db}_test"
    if not test_db.replace("_", "").isalnum():
        raise RuntimeError(f"Unsafe test database name: {test_db}")
    admin_url = url.set(database="postgres").render_as_string(hide_password=False)
    admin_engine = create_async_engine(admin_url, echo=False, isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.begin() as conn:
            exists = (await conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": test_db},
            )).scalar_one_or_none()
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{test_db}"'))
    finally:
        await admin_engine.dispose()
    return url.set(database=test_db).render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def event_loop():
    """Single event-loop for the whole test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ──────────────────────────────────────────────────────────────────────
# In-memory database fixtures
# ──────────────────────────────────────────────────────────────────────
@pytest_asyncio.fixture(scope="function")
async def db_engine():
    """Fresh isolated DB namespace per test."""
    from app.database import Base

    database_url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL", "")
    if database_url.startswith("postgresql"):
        database_url = await _postgres_test_database_url(database_url)
        schema = f"test_{uuid.uuid4().hex}"
        admin_engine = create_async_engine(database_url, echo=False, isolation_level="AUTOCOMMIT")
        async with admin_engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await admin_engine.dispose()

        engine = create_async_engine(
            database_url,
            echo=False,
            connect_args={"server_settings": {"search_path": schema}},
        )
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            yield engine
        finally:
            await engine.dispose()
            cleanup_engine = create_async_engine(database_url, echo=False, isolation_level="AUTOCOMMIT")
            async with cleanup_engine.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await cleanup_engine.dispose()
        return

    try:
        import aiosqlite  # noqa: F401
    except ModuleNotFoundError:
        pytest.skip("aiosqlite is required for local SQLite tests; use PostgreSQL TEST_DATABASE_URL")

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def db_session(db_engine):
    """Async SQLAlchemy session bound to the in-memory DB."""
    Session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        yield session


@pytest_asyncio.fixture(scope="function")
async def session_factory(db_engine):
    """session-maker (callable) for fixtures that need to open multiple sessions."""
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


# ──────────────────────────────────────────────────────────────────────
# Common DB objects
# ──────────────────────────────────────────────────────────────────────
@pytest_asyncio.fixture
async def tenant(db_session):
    from app.database import Tenant
    from app.crypto import encrypt_token
    t = Tenant(
        bitrix_domain="test.bitrix24.ru",
        access_token_encrypted=encrypt_token("test_access_token"),
        refresh_token_encrypted=encrypt_token("test_refresh_token"),
        status="active",
    )
    db_session.add(t)
    await db_session.commit()
    await db_session.refresh(t)
    return t


@pytest_asyncio.fixture
async def tg_account(db_session, tenant):
    from app.database import TelegramAccount
    acc = TelegramAccount(
        tenant_id=tenant.id,
        api_id=12345,
        api_hash="testhash",
        phone="+79001234567",
        username="testuser",
        session_string="test_session",
        bitrix_line_id=30,
        bitrix_connector_id="tg_6",
        state="active",
    )
    db_session.add(acc)
    await db_session.commit()
    await db_session.refresh(acc)
    return acc


@pytest_asyncio.fixture
async def channel(db_session, tenant):
    from app.database import Channel
    ch = Channel(
        tenant_id=tenant.id,
        provider="tg_6",
        name="Test Telegram",
        bitrix_line_id=30,
        status="active",
    )
    db_session.add(ch)
    await db_session.commit()
    await db_session.refresh(ch)
    return ch


@pytest_asyncio.fixture
async def chat_link(db_session, tenant, channel):
    from app.database import ChatLink
    account_id = None
    try:
        from app.database import TelegramAccount
        account = (await db_session.execute(
            select(TelegramAccount)
            .where(TelegramAccount.tenant_id == tenant.id)
            .limit(1)
        )).scalar_one_or_none()
        account_id = account.id if account else None
    except Exception:
        account_id = None
    link = ChatLink(
        tenant_id=tenant.id,
        provider="tg_6",
        external_chat_id="111222333",
        external_user_id="111222333",
        external_user_name="Test User (@testrecipient)",
        bitrix_line_id=30,
        bitrix_chat_id="bx_chat_42",
        account_id=account_id,
    )
    db_session.add(link)
    await db_session.commit()
    await db_session.refresh(link)
    return link


# ──────────────────────────────────────────────────────────────────────
# Mock Bitrix24 client
# ──────────────────────────────────────────────────────────────────────
@pytest.fixture
def mock_bx():
    """Mocks app.bitrix.client module functions."""
    with patch("app.bitrix.client.send_messages", new_callable=AsyncMock) as send_messages, \
         patch("app.bitrix.client.register_connector", new_callable=AsyncMock) as reg_conn, \
         patch("app.bitrix.client.activate_connector", new_callable=AsyncMock) as act_conn, \
         patch("app.bitrix.client.set_connector_data", new_callable=AsyncMock) as set_data, \
         patch("app.bitrix.client.bind_event", new_callable=AsyncMock) as bind_ev, \
         patch("app.bitrix.client.send_delivery", new_callable=AsyncMock) as send_del, \
         patch("app.bitrix.client.get_open_lines", new_callable=AsyncMock) as get_lines:
        yield {
            "send_messages": send_messages,
            "register_connector": reg_conn,
            "activate_connector": act_conn,
            "set_connector_data": set_data,
            "bind_event": bind_ev,
            "send_delivery": send_del,
            "get_open_lines": get_lines,
        }


# ──────────────────────────────────────────────────────────────────────
# Mock Telegram service
# ──────────────────────────────────────────────────────────────────────
@pytest.fixture
def mock_tg_service():
    """Mocks app.telegram.service functions."""
    with patch("app.telegram.service.send_by_entity", new_callable=AsyncMock) as send_by, \
         patch("app.telegram.service.send_file", new_callable=AsyncMock) as send_file, \
         patch("app.telegram.service._active_clients", {}) as active:
        send_by.return_value = {"success": True}
        send_file.return_value = {"success": True}
        yield {
            "send_by_entity": send_by,
            "send_file": send_file,
            "active_clients": active,
        }
