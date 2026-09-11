import asyncio
from logging.config import fileConfig

from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from geoalchemy2 import alembic_helpers
from geoalchemy2.types import Geometry

from alembic import context

from backend.app.core.config import settings
from backend.app.db.models import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The app's own settings (GREENPROJECT_DATABASE_URL / .env) are the one
# source of truth for the DB URL -- alembic.ini's own sqlalchemy.url is left
# as a placeholder so there's nothing to keep in sync by hand or accidentally
# point at the wrong database.
config.set_main_option("sqlalchemy.url", settings.database_url)

# Base.metadata drives `alembic revision --autogenerate` -- it diffs this
# against the live DB schema to draft a migration, so it has to be the same
# Base every model in backend/app/db/models.py declares against.
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def _compare_type(context, inspected_column, metadata_column, inspected_type, metadata_type):
    """Never flag a Geometry column as "changed". Reflecting one back from
    the live DB (what autogenerate/`alembic check` diffs against) doesn't
    reliably recover the srid our model declares (`srid=0`) -- Postgres
    doesn't always store it in a way geoalchemy2's reflection can read back,
    so a column that was created correctly from our model reflects as "srid
    unset" and reads as a permanent, spurious diff on every future run. A
    real change to a Geometry column's type/srid has to be written by hand
    in its own migration -- autogenerate can't be trusted to notice it either
    way, so there's no accuracy actually being traded away here.
    """
    if isinstance(inspected_type, Geometry) and isinstance(metadata_type, Geometry):
        return False
    return None  # fall through to Alembic's own default comparison for everything else


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=alembic_helpers.include_object,
        render_item=alembic_helpers.render_item,
        compare_type=_compare_type,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    # The postgis/postgis image's default user has `tiger`/`topology`
    # (the tiger geocoder + topology extensions it installs) ahead of
    # `public` in its search_path -- reflection without this ends up
    # "seeing" every geocoder table (tract/county/place/edges/faces/...) as
    # if it lived in our own default schema, since SQLAlchemy resolves
    # unqualified table names through the connection's search_path. That
    # blew up an early --autogenerate into a 700+ line migration proposing
    # to drop dozens of unrelated extension tables. Pinning search_path to
    # just `public` keeps reflection scoped to tables we actually own.
    connection.execute(text("SET search_path TO public"))

    # include_object/render_item (from geoalchemy2) keep autogenerate from
    # (a) proposing to DROP PostGIS's own system tables, e.g. spatial_ref_sys
    # -- it lives in the public schema right alongside our own tables and
    # isn't part of Base.metadata, so without this filter a diff against the
    # live DB reads as "this table shouldn't exist" -- and (b) mis-rendering
    # our Geometry(geometry_type="GEOMETRY", srid=0) columns as a generic
    # SQLAlchemy type instead of geoalchemy2's own.
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=alembic_helpers.include_object,
        render_item=alembic_helpers.render_item,
        compare_type=_compare_type,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine
    and associate a connection with the context.

    """

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
        # Explicit, even though context.begin_transaction() manages its own
        # transaction around the migration -- SQLAlchemy 2.x Core
        # Connections default to "commit as you go" and roll back whatever
        # wasn't explicitly committed once the `async with` block above
        # exits, and empirically (this async template verbatim from
        # `alembic init -t async`, against asyncpg + NullPool) that rollback
        # was winning the race: `alembic upgrade head` logged success and
        # stamped alembic_version, but the actual CREATE TABLEs never
        # persisted. Committing here explicitly closes that gap.
        await connection.commit()

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
