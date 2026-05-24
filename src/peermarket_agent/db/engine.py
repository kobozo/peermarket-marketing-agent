"""Async SQLAlchemy engine factory."""

from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from peermarket_agent.config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(settings.agent_db_url, future=True, pool_pre_ping=True)
