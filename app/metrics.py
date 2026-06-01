import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import MetricsResponse, ZoneDwell

logger = logging.getLogger(__name__)

async def get_store_metrics(store_id: str, db: AsyncSession, window_hours: int = 24) -> MetricsResponse:
    """Compute real-time unified operational metrics for a target retail footprint."""
    now = datetime.now(tz=timezone.utc)
    window_start = now - timedelta(hours=window_hours)
    
    params = {
        "store_id": store_id,
        "window_start": window_start,
        "window_end": now,
        "queue_cutoff": now - timedelta(minutes=15)
    }
    
    uv_result = await db.execute(text("""
        SELECT COUNT(DISTINCT visitor_id)
        FROM events
        WHERE store_id    = :store_id
          AND ts          BETWEEN :window_start AND :window_end
          AND is_staff    = false
          AND event_type  IN ('ENTRY', 'REENTRY', 'ZONE_ENTER')
    """), params)
    unique_visitors = uv_result.scalar() or 0
 
    conv_result = await db.execute(text("""
        SELECT
            COUNT(DISTINCT visitor_id) FILTER (WHERE event_type = 'BILLING_QUEUE_JOIN') AS conversions,
            COUNT(DISTINCT visitor_id) AS total_visitors
        FROM events
        WHERE store_id   = :store_id
          AND ts         BETWEEN :window_start AND :window_end
          AND is_staff   = false
    """), params)
    conv_row = conv_result.fetchone()
    conversions = conv_row[0] or 0
    total_visitors = conv_row[1] or 0
    conversion_rate = (conversions / total_visitors if total_visitors > 0 else 0.0)
 
    dwell_result = await db.execute(text("""
        SELECT
            zone_id,
            AVG(dwell_ms)::float    AS avg_dwell,
            COUNT(*)                AS visit_count
        FROM events
        WHERE store_id   = :store_id
          AND ts         BETWEEN :window_start AND :window_end
          AND is_staff   = false
          AND event_type IN ('ZONE_DWELL', 'ZONE_EXIT')
          AND zone_id    IS NOT NULL
          AND dwell_ms   > 0
        GROUP BY zone_id
        ORDER BY avg_dwell DESC
    """), params)
    
    zone_dwells = [
        ZoneDwell(
            zone_id=row[0],
            avg_dwell_ms=round(row[1], 1),
            visit_count=row[2],
        )
        for row in dwell_result.fetchall()
    ]
 
    avg_dwell_ms = (
        sum(z.avg_dwell_ms * z.visit_count for z in zone_dwells)
        / max(sum(z.visit_count for z in zone_dwells), 1)
    ) if zone_dwells else 0.0
 
    # FIXED: Replaced non-deterministic database engine NOW() checks with our parameterized boundary anchor
    queue_result = await db.execute(text("""
        WITH billing_joins AS (
            SELECT visitor_id, MAX(ts) AS joined_at
            FROM events
            WHERE store_id   = :store_id
              AND event_type = 'BILLING_QUEUE_JOIN'
              AND ts         >= :queue_cutoff
            GROUP BY visitor_id
        ),
        billing_abandons AS (
            SELECT visitor_id, MAX(ts) AS abandoned_at
            FROM events
            WHERE store_id   = :store_id
              AND event_type = 'BILLING_QUEUE_ABANDON'
              AND ts         >= :queue_cutoff
            GROUP BY visitor_id
        )
        SELECT COUNT(*)
        FROM billing_joins j
        LEFT JOIN billing_abandons a USING (visitor_id)
        WHERE a.visitor_id IS NULL
           OR j.joined_at > a.abandoned_at
    """), params)
    queue_depth = queue_result.scalar() or 0
 
    abandon_result = await db.execute(text("""
        SELECT
            COUNT(*) FILTER (WHERE event_type = 'BILLING_QUEUE_ABANDON') AS abandons,
            COUNT(*) FILTER (WHERE event_type = 'BILLING_QUEUE_JOIN')    AS joins
        FROM events
        WHERE store_id   = :store_id
          AND ts         BETWEEN :window_start AND :window_end
          AND is_staff   = false
    """), params)
    ab_row = abandon_result.fetchone()
    abandons = ab_row[0] or 0
    joins = ab_row[1] or 0
    abandonment_rate = abandons / joins if joins > 0 else 0.0
 
    data_confidence = unique_visitors >= 20
 
    return MetricsResponse(
        store_id=store_id,
        window_start=window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        window_end=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        unique_visitors=unique_visitors,
        conversion_rate=round(conversion_rate, 4),
        avg_dwell_ms=round(avg_dwell_ms, 1),
        zone_dwells=zone_dwells,
        queue_depth=queue_depth,
        abandonment_rate=round(abandonment_rate, 4),
        data_confidence=data_confidence,
    )