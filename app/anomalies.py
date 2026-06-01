import logging
import math
import os
from datetime import datetime, timedelta, timezone
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Anomaly, AnomaliesResponse
 
logger = logging.getLogger(__name__)
QUEUE_SPIKE_DEPTH = int(os.getenv("QUEUE_SPIKE_DEPTH", "5"))
CONVERSION_DROP_STDDEV= float(os.getenv("CONVERSION_DROP_STDDEV", "2.0"))
DEAD_ZONE_MINUTES = int(os.getenv("DEAD_ZONE_MINUTES", "30"))
STALE_FEED_MINUTES = int(os.getenv("STALE_FEED_MINUTES", "10"))
 
 
async def get_store_anomalies(store_id: str,db: AsyncSession) -> AnomaliesResponse:
    now = datetime.now(tz=timezone.utc)
    anomalies: list[Anomaly] = []
 
    await _check_queue_spike(store_id, db, now, anomalies)
    await _check_conversion_drop(store_id, db, now, anomalies)
    await _check_dead_zones(store_id, db, now, anomalies)
    await _check_stale_feed(store_id, db, now, anomalies)
 
    return AnomaliesResponse(store_id=store_id, anomalies=anomalies)
 

async def _check_queue_spike(store_id: str, db: AsyncSession, now: datetime, anomalies: list[Anomaly]) -> None:
    """ check queue spike in stores"""
    result = await db.execute(text("""
        WITH joins AS (
            SELECT visitor_id, MAX(ts) AS joined_at
            FROM events
            WHERE store_id   = :store_id
              AND event_type = 'BILLING_QUEUE_JOIN'
              AND ts         >= :cutoff
            GROUP BY visitor_id
        ),
        abandons AS (
            SELECT visitor_id, MAX(ts) AS abandoned_at
            FROM events
            WHERE store_id   = :store_id
              AND event_type = 'BILLING_QUEUE_ABANDON'
              AND ts         >= :cutoff
            GROUP BY visitor_id
        )
        SELECT COUNT(*) AS queue_depth
        FROM joins j
        LEFT JOIN abandons a USING (visitor_id)
        WHERE a.visitor_id IS NULL OR j.joined_at > a.abandoned_at
    """), {
        "store_id": store_id,
        "cutoff": now - timedelta(minutes=15),
    })
    queue_depth = result.scalar() or 0
 
    if queue_depth >= QUEUE_SPIKE_DEPTH:
        severity = "CRITICAL" if queue_depth >= QUEUE_SPIKE_DEPTH * 2 else "WARN"
        anomalies.append(Anomaly(
            anomaly_type="BILLING_QUEUE_SPIKE",
            severity=severity,
            description=f"Billing queue depth is {queue_depth} (threshold: {QUEUE_SPIKE_DEPTH})",
            suggested_action=(
                "Open an additional billing counter or redirect staff to assist customers."
            ),
            detected_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            value=float(queue_depth),
            threshold=float(QUEUE_SPIKE_DEPTH),
        ))
 
 
async def _check_conversion_drop(store_id: str, db: AsyncSession, now: datetime, anomalies: list[Anomaly]) -> None:
    """check for conversion drop on store"""
    today = now.date().isoformat()
 
    # 7-day history (excluding today)
    history_result = await db.execute(text("""
        SELECT conversion_rate
        FROM daily_metric_cache
        WHERE store_id = :store_id
          AND date     < :today
          AND date     >= :week_ago
        ORDER BY date DESC
        LIMIT 7
    """), {
        "store_id": store_id,
        "today":    today,
        "week_ago": (now - timedelta(days=7)).date().isoformat(),
    })
    history = [float(r[0]) for r in history_result.fetchall()]
 
    if len(history) < 3:
        # Not enough history to make a meaningful comparison
        return
 
    mean_rate = sum(history) / len(history)
    variance  = sum((r - mean_rate) ** 2 for r in history) / len(history)
    std_rate  = math.sqrt(variance)
 
    # Today's live conversion rate
    today_result = await db.execute(text("""
        SELECT
            COUNT(DISTINCT visitor_id) FILTER (WHERE event_type = 'BILLING_QUEUE_JOIN') AS conversions,
            COUNT(DISTINCT visitor_id) AS total
        FROM events
        WHERE store_id = :store_id
          AND DATE(ts AT TIME ZONE 'UTC') = :today::date
          AND is_staff = false
    """), {"store_id": store_id, "today": today})
    today_row = today_result.fetchone()
    today_conv  = today_row[0] or 0
    today_total = today_row[1] or 0
    today_rate  = today_conv / today_total if today_total > 0 else 0.0
 
    threshold = mean_rate - CONVERSION_DROP_STDDEV * std_rate
 
    if today_rate < threshold and today_total >= 10:
        drop_pct = round((mean_rate - today_rate) / mean_rate * 100, 1) if mean_rate > 0 else 0
        anomalies.append(Anomaly(
            anomaly_type="CONVERSION_DROP",
            severity="WARN",
            description=(
                f"Today's conversion rate {today_rate:.1%} is {drop_pct}% below "
                f"the 7-day average of {mean_rate:.1%}"
            ),
            suggested_action=(
                "Check for staffing gaps on the floor or product availability issues "
                "near the billing zone. Review any ongoing promotions."
            ),
            detected_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            value=round(today_rate, 4),
            threshold=round(threshold, 4),
        ))
 
 
async def _check_dead_zones(store_id: str, db: AsyncSession, now: datetime, anomalies: list[Anomaly] ) -> None:
    """ checking dead zones ( no visitors visted for 2 days)"""
    cutoff = now - timedelta(minutes=DEAD_ZONE_MINUTES)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
 
    # Zones active today
    active_today_result = await db.execute(text("""
        SELECT DISTINCT zone_id
        FROM events
        WHERE store_id   = :store_id
          AND ts         >= :day_start
          AND zone_id    IS NOT NULL
          AND zone_id    NOT IN ('BILLING', 'ENTRY_ZONE')
          AND is_staff   = false
          AND event_type = 'ZONE_ENTER'
    """), {"store_id": store_id, "day_start": day_start})
    active_today = {r[0] for r in active_today_result.fetchall()}
 
    if not active_today:
        return
 
    # Zones with recent activity
    recent_result = await db.execute(text("""
        SELECT DISTINCT zone_id
        FROM events
        WHERE store_id   = :store_id
          AND ts         >= :cutoff
          AND zone_id    IS NOT NULL
          AND is_staff   = false
          AND event_type = 'ZONE_ENTER'
    """), {"store_id": store_id, "cutoff": cutoff})
    recently_active = {r[0] for r in recent_result.fetchall()}
 
    dead_zones = active_today - recently_active
    for zone_id in sorted(dead_zones):
        anomalies.append(Anomaly(
            anomaly_type="DEAD_ZONE",
            severity="INFO",
            description=(
                f"Zone '{zone_id}' had no customer visits in the last "
                f"{DEAD_ZONE_MINUTES} minutes despite earlier activity today."
            ),
            suggested_action=(
                f"Check if zone '{zone_id}' needs restocking, better signage, "
                "or staff engagement to drive traffic."
            ),
            detected_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ))
 
 
async def _check_stale_feed(store_id: str, db: AsyncSession, now: datetime, anomalies: list[Anomaly]) -> None:
    """Detect if the event feed for this store has gone silent."""
    result = await db.execute(text("""
        SELECT MAX(ts)
        FROM events
        WHERE store_id = :store_id
    """), {"store_id": store_id})
 
    last_ts = result.scalar()
    if last_ts is None:
        # Never received any events — not a stale feed, just no data yet
        return
 
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=timezone.utc)
 
    lag_seconds = (now - last_ts).total_seconds()
 
    if lag_seconds > STALE_FEED_MINUTES * 60:
        anomalies.append(Anomaly(
            anomaly_type="STALE_FEED",
            severity="CRITICAL",
            description=(
                f"No events received for store '{store_id}' in the last "
                f"{lag_seconds / 60:.1f} minutes (threshold: {STALE_FEED_MINUTES} min)."
            ),
            suggested_action=(
                "Check camera connectivity, the detection pipeline process, "
                "and network/ingest endpoint availability."
            ),
            detected_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            value=round(lag_seconds, 1),
            threshold=float(STALE_FEED_MINUTES * 60),
        ))
 