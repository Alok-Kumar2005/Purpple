from datetime import datetime, timedelta, timezone
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import HeatmapResponse, HeatmapZone
 
 
async def get_store_heatmap(store_id: str,db: AsyncSession,window_hours: int = 24) -> HeatmapResponse:
    now = datetime.now(tz=timezone.utc)
    window_start = now - timedelta(hours=window_hours)
 
    params = {
        "store_id": store_id,
        "window_start": window_start,
        "window_end": now,
    }
 
    result = await db.execute(text("""
        SELECT
            zone_id,
            COUNT(DISTINCT visitor_id)  AS visit_count,
            AVG(dwell_ms)::float        AS avg_dwell_ms
        FROM events
        WHERE store_id   = :store_id
          AND ts         BETWEEN :window_start AND :window_end
          AND is_staff   = false
          AND zone_id    IS NOT NULL
          AND event_type IN ('ZONE_ENTER', 'ZONE_DWELL', 'ZONE_EXIT')
        GROUP BY zone_id
        ORDER BY visit_count DESC
    """), params)
 
    rows = result.fetchall()
 
    if not rows:
        return HeatmapResponse(
            store_id=store_id,
            window=f"{window_start.strftime('%Y-%m-%dT%H:%M:%SZ')} / {now.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            zones=[],
            data_confidence=False,
        )
 
    max_visits = max(r[1] for r in rows) or 1
    total_sessions = sum(r[1] for r in rows)
 
    zones = [
        HeatmapZone(
            zone_id=row[0],
            visit_count=row[1],
            avg_dwell_ms=round(row[2] or 0.0, 1),
            normalised=round((row[1] / max_visits) * 100, 1),
        )
        for row in rows
    ]
 
    return HeatmapResponse(
        store_id=store_id,
        window=f"{window_start.strftime('%Y-%m-%dT%H:%M:%SZ')} / {now.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        zones=zones,
        data_confidence=(total_sessions >= 20),
    )
 