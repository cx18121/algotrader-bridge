"""Async SQLAlchemy engine and session factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


_engine = None
_SessionFactory: async_sessionmaker[AsyncSession] | None = None


def _get_url() -> str:
    return f"sqlite+aiosqlite:///{settings().db_path}"


def engine():
    global _engine
    if _engine is None:
        _engine = create_async_engine(_get_url(), echo=False, future=True)
    return _engine


def session_factory() -> async_sessionmaker[AsyncSession]:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = async_sessionmaker(engine(), expire_on_commit=False, class_=AsyncSession)
    return _SessionFactory


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    factory = session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Create all tables if they do not exist. Imports models for side-effect."""
    from . import models  # noqa: F401 -- register ORM classes on Base.metadata

    async with engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("db_initialized", extra={"url": _get_url()})


async def dispose_db() -> None:
    global _engine, _SessionFactory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _SessionFactory = None
