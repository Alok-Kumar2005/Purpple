import asyncio
import os

import pytest
import pytest_asyncio
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Must be set before app.database is imported by test_api.py
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://retail:retail@localhost:5433/store_intelligence_test",
)

from app.database import Base          # noqa: E402  (needs env var above)
import app.database as _db_module      # noqa: E402


def _make_test_engine():
    return create_async_engine(
        os.environ["DATABASE_URL"],
        poolclass=NullPool,            # ← the key fix
        echo=False,
    )


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _patch_engine(event_loop):
    """
    Replace the module-level engine and session factory with NullPool versions
    *after* the session event loop is running, so all connections belong to
    the correct loop.
    """
    engine = _make_test_engine()
    session_factory = async_sessionmaker(
        bind=engine, class_=AsyncSession,
        expire_on_commit=False, autoflush=False,
    )

    # Patch the module globals that the rest of the app imports
    _db_module.engine = engine
    _db_module.AsyncSessionLocal = session_factory

    # Create schema
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield

    # Tear down
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()