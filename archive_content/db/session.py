from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)

from config import settings
from db.models import Base

engine = create_async_engine(settings.database_url, pool_pre_ping=True, future=True)
SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False, class_=AsyncSession
)


async def init_db() -> None:
    async with engine.begin() as conn:
        # Existing PostgreSQL installations need the new Ukrainian enum value.
        # SQLite does not use a native enum, so this is safely skipped there.
        if conn.dialect.name == "postgresql":
            exists = await conn.exec_driver_sql(
                "SELECT 1 FROM pg_type WHERE typname = 'language_code'"
            )
            if exists.first() is not None:
                await conn.exec_driver_sql("ALTER TYPE language_code ADD VALUE IF NOT EXISTS 'uk'")
        await conn.run_sync(Base.metadata.create_all)
