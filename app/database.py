import os
from typing import AsyncGenerator
 
from sqlalchemy import (
    Boolean, Column, DateTime, Float, Index, Integer, String, UniqueConstraint
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from dotenv import load_dotenv
load_dotenv()  
 
DATABASE_URL = os.environ.get("DATABASE_URL")
 

class Base(DeclarativeBase):
    pass
 

class Event(Base):
    __tablename__ = "events"
 
    id          = Column(Integer, primary_key=True, autoincrement=True)
    event_id    = Column(String(36), nullable=False)
    store_id    = Column(String(64), nullable=False)
    camera_id   = Column(String(64), nullable=False)
    visitor_id  = Column(String(64), nullable=False)
    event_type  = Column(String(32), nullable=False)
    ts          = Column(DateTime(timezone=True), nullable=False)
    zone_id     = Column(String(64), nullable=True)
    dwell_ms    = Column(Integer, nullable=False, default=0)
    is_staff    = Column(Boolean, nullable=False, default=False)
    confidence  = Column(Float, nullable=False)
    metadata_   = Column("metadata", JSONB, nullable=False, default=dict)
 
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_event_id"),
        Index("ix_events_store_ts",     "store_id", "ts"),
        Index("ix_events_store_type",   "store_id", "event_type"),
        Index("ix_events_visitor",      "store_id", "visitor_id"),
        Index("ix_events_zone",         "store_id", "zone_id"),
        Index("ix_events_camera_ts",    "camera_id", "ts"),
    )
 
 
class DailyMetricCache(Base):
    __tablename__ = "daily_metric_cache"
 
    id                  = Column(Integer, primary_key=True, autoincrement=True)
    store_id            = Column(String(64), nullable=False)
    date                = Column(String(10), nullable=False)
    unique_visitors     = Column(Integer, nullable=False, default=0)
    conversions         = Column(Integer, nullable=False, default=0)
    conversion_rate     = Column(Float, nullable=False, default=0.0)
    avg_dwell_ms        = Column(Float, nullable=False, default=0.0)
    abandonment_count   = Column(Integer, nullable=False, default=0)
 
    __table_args__ = (
        UniqueConstraint("store_id", "date", name="uq_store_date"),
        Index("ix_daily_store_date", "store_id", "date"),
    )
 
 
engine = create_async_engine(DATABASE_URL, pool_size=10, max_overflow=20, pool_pre_ping=True, echo=False)
AsyncSessionLocal = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
 
 
async def create_tables() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
 
 
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise