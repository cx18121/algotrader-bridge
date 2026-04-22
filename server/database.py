"""Async SQLAlchemy engine and session factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
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

        @event.listens_for(_engine.sync_engine, "connect")
        def _set_wal(dbapi_conn, _record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

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
    import json
    import os
    import re

    from sqlalchemy import select

    from . import models  # noqa: F401 -- register ORM classes on Base.metadata

    async with engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Additive migrations for columns added after initial deployment.
        for sql in (
            "ALTER TABLE positions ADD COLUMN close_fill_price REAL",
            "ALTER TABLE positions ADD COLUMN signal_entry_price REAL",
            "ALTER TABLE trade_history ADD COLUMN signal_entry_price REAL",
            "ALTER TABLE trade_history ADD COLUMN signal_close_price REAL",
        ):
            try:
                await conn.execute(text(sql))
            except OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise

    # Seed ContractMap from CONTRACT_MAP env if the table is empty.
    raw_map = os.getenv("CONTRACT_MAP", "").strip()
    if raw_map:
        try:
            parsed = json.loads(raw_map)
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict) and parsed:
            async with session_factory()() as sess:
                existing = await sess.execute(select(models.ContractMap))
                if not existing.scalars().first():
                    for tv_sym, spec in parsed.items():
                        if not isinstance(spec, dict):
                            continue
                        base = re.sub(r"\d+!$", "", tv_sym.upper())
                        sess.add(models.ContractMap(
                            tv_symbol=base,
                            ib_symbol=spec.get("symbol", base),
                            sec_type=spec.get("sec_type", "stock"),
                            exchange=spec.get("exchange", "SMART"),
                            currency=spec.get("currency", "USD"),
                            last_trade_date=spec.get("last_trade_date"),
                        ))
                    await sess.commit()
                    log.info("contract_map_seeded_from_env")

    log.info("db_initialized", extra={"url": _get_url()})


async def dispose_db() -> None:
    global _engine, _SessionFactory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _SessionFactory = None
