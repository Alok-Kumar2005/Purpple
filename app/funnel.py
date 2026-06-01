import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import FunnelResponse, FunnelStage
 
logger = logging.getLogger(__name__)

STAGE_LABELS = ["ENTRY", "ZONE_VISIT", "BILLING_QUEUE", "PURCHASE"]
 
 
async def get_store_funnel(store_id: str, db: AsyncSession, window_hours: int = 24) -> FunnelResponse:
    now = datetime.now(tz=timezone.utc)
    window_start = now - timedelta(hours=window_hours)
    params = {
        "store_id": store_id,
        "window_start": window_start,
        "window_end": now,
    }
 
    result = await db.execute(text("""
        WITH visitor_flags AS (
            SELECT
                visitor_id,
                -- Stage 1: entered the store
                BOOL_OR(event_type IN ('ENTRY', 'REENTRY'))             AS entered,
                -- Stage 2: visited at least one zone
                BOOL_OR(event_type = 'ZONE_ENTER')                      AS zone_visited,
                -- Stage 3: reached billing queue
                BOOL_OR(event_type = 'BILLING_QUEUE_JOIN')              AS reached_billing,
                -- Stage 4: did NOT abandon (proxy for purchase)
                -- A visitor purchased if they joined billing but never abandoned
                (
                    BOOL_OR(event_type = 'BILLING_QUEUE_JOIN')
                    AND NOT BOOL_OR(event_type = 'BILLING_QUEUE_ABANDON')
                )                                                        AS purchased
            FROM events
            WHERE store_id  = :store_id
              AND ts         BETWEEN :window_start AND :window_end
              AND is_staff   = false
            GROUP BY visitor_id
        )
        SELECT
            COUNT(*) FILTER (WHERE entered)         AS stage_entry,
            COUNT(*) FILTER (WHERE zone_visited)    AS stage_zone,
            COUNT(*) FILTER (WHERE reached_billing) AS stage_billing,
            COUNT(*) FILTER (WHERE purchased)       AS stage_purchase
        FROM visitor_flags
    """), params)
 
    row = result.fetchone()
    counts = [
        row[0] or 0, # ENTRY
        row[1] or 0, # ZONE_VISIT
        row[2] or 0, # BILLING_QUEUE
        row[3] or 0, # PURCHASE
    ]
 
    stages: list[FunnelStage] = []
    for i, (label, count) in enumerate(zip(STAGE_LABELS, counts)):
        if i == 0:
            drop_off_pct = 0.0
        else:
            prev = counts[i - 1]
            drop_off_pct = round((1.0 - count / prev) * 100, 1) if prev > 0 else 0.0
 
        stages.append(FunnelStage(
            stage=label,
            count=count,
            drop_off_pct=drop_off_pct,
        ))
 
    window_label = (
        f"{window_start.strftime('%Y-%m-%dT%H:%M:%SZ')} / "
        f"{now.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )
 
    return FunnelResponse(
        store_id=store_id,
        window=window_label,
        stages=stages,
    )
 