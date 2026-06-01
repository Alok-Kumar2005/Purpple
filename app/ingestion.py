import json
import logging
from datetime import datetime
from typing import Any
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException
from datetime import date as dt_date
from app.models import InboundEvent, IngestRequest, IngestResponse
 
logger = logging.getLogger(__name__)
 
async def ingest_batch(payload_dict: dict, db: AsyncSession) -> IngestResponse:
    """Validate and ingest events individually to support partial batch success."""
    raw_events = payload_dict.get("events", [])
    
    # Enforce batch constraints manually to return a standard 422 if empty or too large
    if not raw_events or len(raw_events) > 500:
        from fastapi.exceptions import RequestValidationError
        from pydantic import ValidationError
        raise HTTPException(status_code=422, detail="Batch size must be between 1 and 500 events.")

    accepted = 0
    duplicates = 0
    rejected = 0
    errors: list[dict[str, Any]] = []
    rows_to_insert: list[dict] = []
    
    for raw in raw_events:
        try:
            # Validate each event individually
            validated_event = InboundEvent.model_validate(raw)
            row = _event_to_row(validated_event)
            rows_to_insert.append(row)
        except Exception as exc:
            rejected += 1
            errors.append({
                "event_id": raw.get("event_id", "unknown") if isinstance(raw, dict) else "unknown",
                "error": str(exc),
            })
 
    if not rows_to_insert:
        return IngestResponse(accepted=accepted, duplicates=duplicates, rejected=rejected, errors=errors)
 
    inserted_ids: set[str] = set()
    try:
        stmt = text("""
            INSERT INTO events
                (event_id, store_id, camera_id, visitor_id, event_type,
                 ts, zone_id, dwell_ms, is_staff, confidence, metadata)
            VALUES
                (:event_id, :store_id, :camera_id, :visitor_id, :event_type,
                 :ts, :zone_id, :dwell_ms, :is_staff, :confidence, CAST(:metadata AS jsonb))
            ON CONFLICT (event_id) DO NOTHING
            RETURNING event_id
        """)
 
        for row in rows_to_insert:
            result = await db.execute(stmt, row)
            returned = result.fetchone()
            if returned:
                inserted_ids.add(returned[0])
        await db.flush()
    except Exception as exc:
        logger.error("Bulk insert failed: %s", exc)
        await db.rollback()
        return IngestResponse(accepted=0, duplicates=0, rejected=len(rows_to_insert), errors=[{"error": str(exc)}])
 
    accepted = len(inserted_ids)
    duplicates = len(rows_to_insert) - accepted
 
    affected_stores = {r["store_id"] for r in rows_to_insert}
    affected_dates = {r["ts"].date().isoformat() for r in rows_to_insert}
 
    for store_id in affected_stores:
        for date in affected_dates:
            try:
                await _refresh_daily_cache(db, store_id, date)
            except Exception as exc:
                logger.warning("Cache refresh failed for %s / %s: %s", store_id, date, exc)
 
    return IngestResponse(accepted=accepted, duplicates=duplicates, rejected=rejected, errors=errors)
 
def _event_to_row(event: InboundEvent) -> dict:
    """Convert a validated InboundEvent into a dict ready for INSERT."""
    ts = datetime.fromisoformat(event.timestamp.replace("Z", "+00:00"))
    return {
        "event_id": event.event_id,
        "store_id": event.store_id,
        "camera_id": event.camera_id,
        "visitor_id": event.visitor_id,
        "event_type": event.event_type,
        "ts": ts,
        "zone_id": event.zone_id,
        "dwell_ms": event.dwell_ms,
        "is_staff": event.is_staff,
        "confidence": event.confidence,
        "metadata": json.dumps(event.metadata.model_dump()),
    }
 
async def _refresh_daily_cache(db: AsyncSession, store_id: str, date: str) -> None:
    """Regenerate analytics rollups safely bypassing run-time execution overhead."""
    stmt = text("""
        WITH sessions AS (
            SELECT
                visitor_id,
                COUNT(*) FILTER (WHERE event_type = 'ENTRY') AS entries,
                COUNT(*) FILTER (WHERE event_type = 'BILLING_QUEUE_JOIN') AS checkout_joins,
                COUNT(*) FILTER (WHERE event_type = 'BILLING_QUEUE_ABANDON') AS abandons,
                MAX(dwell_ms) AS max_dwell
            FROM events
            WHERE store_id = :store_id
              AND DATE(ts AT TIME ZONE 'UTC') = CAST(:date AS DATE)
              AND is_staff = false
            GROUP BY visitor_id
        )
        INSERT INTO daily_metric_cache
            (store_id, date, unique_visitors, conversions, conversion_rate,
             avg_dwell_ms, abandonment_count)
        SELECT
            :store_id,
            :date,
            COUNT(*)                                            AS unique_visitors,
            COUNT(*) FILTER (WHERE checkout_joins > 0)          AS conversions,
            CASE WHEN COUNT(*) > 0
                 THEN COUNT(*) FILTER (WHERE checkout_joins > 0)::float / COUNT(*)
                 ELSE 0 END                                     AS conversion_rate,
            COALESCE(AVG(max_dwell), 0)                        AS avg_dwell_ms,
            COALESCE(SUM(abandons), 0)                         AS abandonment_count
        FROM sessions
        ON CONFLICT (store_id, date) DO UPDATE SET
            unique_visitors   = EXCLUDED.unique_visitors,
            conversions       = EXCLUDED.conversions,
            conversion_rate   = EXCLUDED.conversion_rate,
            avg_dwell_ms      = EXCLUDED.avg_dwell_ms,
            abandonment_count = EXCLUDED.abandonment_count
    """)
 
    # FIXED: Safely parse the string date into a true Python date object 
    # so asyncpg's binary encoder can map it cleanly to the PostgreSQL engine.
    parsed_date = dt_date.fromisoformat(date) if isinstance(date, str) else date

    await db.execute(stmt, {"store_id": store_id, "date": parsed_date})