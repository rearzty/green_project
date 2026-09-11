from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.core.config import settings
from backend.app.db.models import Base

engine = create_async_engine(settings.database_url, future=True)
# expire_on_commit=False: with a sync Session, attributes are transparently
# re-fetched on next access after commit; with AsyncSession that re-fetch
# needs an await, so touching an expired attribute from plain (non-async)
# code -- e.g. building a response after a commit -- would raise. Callers
# that need fresh server-generated values still call `await session.refresh()`
# explicitly (see services/*.py).
SessionLocal = async_sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)


async def init_db() -> None:
    """Dev/demo convenience — creates tables directly from models.
    Once schema churn settles down, switch to Alembic migrations
    (backend/app/db/migrations) instead of calling this in production paths.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
