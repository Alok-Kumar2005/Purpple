import logging
from datetime import datetime, timezone
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import HealthResponse, StoreFeedStatus
 
logger = logging.getLogger(__name__)
 
STALE_FEED_SECONDS = 600   # 10 minutes
 
 
async def get_health(db: AsyncSession) -> HealthResponse:
    now = datetime.now(tz=timezone.utc)
    db_connected = True
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:
        logger.error("DB health check failed: %s", exc)
        db_connected = False
 
    ## per store status
    store_statuses: list[StoreFeedStatus] = []
 
    if db_connected:
        try:
            result = await db.execute(text("""
                SELECT store_id, MAX(ts) AS last_ts
                FROM events
                GROUP BY store_id
                ORDER BY store_id
            """))
            rows = result.fetchall()
 
            for row in rows:
                store_id = row[0]
                last_ts  = row[1]
 
                if last_ts is None:
                    store_statuses.append(StoreFeedStatus(
                        store_id=store_id,
                        last_event_ts=None,
                        lag_seconds=None,
                        status="NO_DATA",
                    ))
                    continue
 
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=timezone.utc)
 
                lag_s = (now - last_ts).total_seconds()
                status = "STALE_FEED" if lag_s > STALE_FEED_SECONDS else "OK"
 
                store_statuses.append(StoreFeedStatus(
                    store_id=store_id,
                    last_event_ts=last_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    lag_seconds=round(lag_s, 1),
                    status=status,
                ))
 
        except Exception as exc:
            logger.error("Store feed query failed: %s", exc)
            db_connected = False
 
    overall = (
        "healthy"
        if db_connected and all(s.status == "OK" for s in store_statuses)
        else "degraded"
    )
 
    return HealthResponse(
        status=overall,
        db_connected=db_connected,
        stores=store_statuses,
        checked_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
 