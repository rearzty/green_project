from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.core.config import REPO_ROOT, settings

engine = create_async_engine(settings.database_url, future=True)
# expire_on_commit=False: with a sync Session, attributes are transparently
# re-fetched on next access after commit; with AsyncSession that re-fetch
# needs an await, so touching an expired attribute from plain (non-async)
# code -- e.g. building a response after a commit -- would raise. Callers
# that need fresh server-generated values still call `await session.refresh()`
# explicitly (see services/*.py).
SessionLocal = async_sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)


def _upgrade_to_head() -> None:
    # Imported lazily, not at module level: alembic.config.Config parses
    # alembic.ini (and, indirectly, migrations/env.py imports this very
    # module) at construction time, so importing alembic eagerly here would
    # risk a circular import for no benefit -- this only ever runs once, at
    # startup.
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    command.upgrade(cfg, "head")


async def init_db() -> None:
    """Applies any pending Alembic migrations (backend/app/db/migrations) up
    to head -- replaces the earlier `Base.metadata.create_all()` dev
    convenience now that real migrations exist. Runs `command.upgrade()` in
    a worker thread: it's a synchronous call, and migrations/env.py's own
    online-migration runner manages its own `asyncio.run()` for the async
    engine internally, which can't nest inside the event loop this
    coroutine is already running on (FastAPI's lifespan).
    """
    await run_in_threadpool(_upgrade_to_head)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
