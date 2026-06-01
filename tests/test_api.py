import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator
 
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from dotenv import load_dotenv
load_dotenv()
 
# ── Point at SQLite before importing app modules ──────────────────────────────
# Change the top database connection line to look for a local testing database:
os.environ["DATABASE_URL"] = os.getenv("TEST_DATABASE_URL")
 
from app.database import Base, engine, AsyncSessionLocal   # noqa: E402
from main import app
 
 
# ── Fixtures ──────────────────────────────────────────────────────────────────
 
@pytest_asyncio.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
 
 
@pytest_asyncio.fixture(scope="session", autouse=True)
async def create_schema():
    """Create all tables once per test session."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
 
 
@pytest_asyncio.fixture
async def db():
    async with AsyncSessionLocal() as session:
        yield session
        await session.rollback()
 
 
@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac
 
 
# ── Event factory ─────────────────────────────────────────────────────────────
 
def make_event(
    store_id: str = "ST_TEST",
    camera_id: str = "CAM_ENTRY_01",
    visitor_id: str | None = None,
    event_type: str = "ENTRY",
    zone_id: str | None = None,
    dwell_ms: int = 0,
    is_staff: bool = False,
    confidence: float = 0.90,
    ts_offset_s: int = 0,
    event_id: str | None = None,
) -> dict:
    base_ts = datetime(2026, 4, 10, 20, 0, 0, tzinfo=timezone.utc)
    ts = (base_ts + timedelta(seconds=ts_offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "event_id":   event_id or str(uuid.uuid4()),
        "store_id":   store_id,
        "camera_id":  camera_id,
        "visitor_id": visitor_id or f"VIS_{uuid.uuid4().hex[:8]}",
        "event_type": event_type,
        "timestamp":  ts,
        "zone_id":    zone_id,
        "dwell_ms":   dwell_ms,
        "is_staff":   is_staff,
        "confidence": confidence,
        "metadata":   {"queue_depth": None, "sku_zone": zone_id, "session_seq": 1},
    }
 
 
# ── POST /events/ingest ───────────────────────────────────────────────────────
 
class TestIngest:
    async def test_valid_batch_accepted(self, client: AsyncClient):
        events = [make_event() for _ in range(5)]
        resp = await client.post("/events/ingest", json={"events": events})
        assert resp.status_code == 200
        body = resp.json()
        assert body["accepted"] == 5
        assert body["duplicates"] == 0
        assert body["rejected"] == 0
 
    async def test_idempotent_second_call_is_all_duplicates(self, client: AsyncClient):
        events = [make_event(event_id="dedup-001"), make_event(event_id="dedup-002")]
        # First call
        r1 = await client.post("/events/ingest", json={"events": events})
        assert r1.json()["accepted"] == 2
 
        # Second call — identical payload
        r2 = await client.post("/events/ingest", json={"events": events})
        body = r2.json()
        assert body["accepted"] == 0
        assert body["duplicates"] == 2
        assert body["rejected"] == 0
 
    async def test_partial_success_malformed_event_rejected(self, client: AsyncClient):
        good = make_event(event_id="partial-good-001")
        bad  = {**make_event(), "event_type": "NOT_A_REAL_TYPE", "event_id": "partial-bad-001"}
        resp = await client.post("/events/ingest", json={"events": [good, bad]})
        assert resp.status_code == 200
        body = resp.json()
        assert body["accepted"] == 1
        assert body["rejected"] == 1
        assert len(body["errors"]) == 1
 
    async def test_batch_too_large_returns_422(self, client: AsyncClient):
        events = [make_event() for _ in range(501)]
        resp = await client.post("/events/ingest", json={"events": events})
        assert resp.status_code == 422
 
    async def test_empty_batch_returns_422(self, client: AsyncClient):
        resp = await client.post("/events/ingest", json={"events": []})
        assert resp.status_code == 422
 
    async def test_response_has_required_keys(self, client: AsyncClient):
        resp = await client.post("/events/ingest", json={"events": [make_event()]})
        body = resp.json()
        for key in ("accepted", "duplicates", "rejected", "errors"):
            assert key in body, f"Missing key: {key}"
 
    async def test_trace_id_header_present(self, client: AsyncClient):
        resp = await client.post("/events/ingest", json={"events": [make_event()]})
        assert "x-trace-id" in resp.headers
 
    async def test_staff_events_ingested_but_counted_as_staff(self, client: AsyncClient):
        events = [make_event(is_staff=True, event_id=f"staff-{i}") for i in range(3)]
        resp = await client.post("/events/ingest", json={"events": events})
        assert resp.status_code == 200
        assert resp.json()["accepted"] == 3   # ingested OK — filtering is at query time
 
 
# ── GET /stores/{id}/metrics ──────────────────────────────────────────────────
 
METRICS_STORE = "ST_METRICS"
 
 
@pytest_asyncio.fixture(autouse=False)
async def seed_metrics(client: AsyncClient):
    """Seed a known set of events for metrics tests."""
    vid1, vid2, vid3 = "VIS_M001", "VIS_M002", "VIS_M003"
    events = [
        # Visitor 1: enters, visits SKINCARE, reaches billing
        make_event(store_id=METRICS_STORE, visitor_id=vid1, event_type="ENTRY",             ts_offset_s=0,   event_id="m-e1"),
        make_event(store_id=METRICS_STORE, visitor_id=vid1, event_type="ZONE_ENTER",        zone_id="SKINCARE", ts_offset_s=60,  event_id="m-e2"),
        make_event(store_id=METRICS_STORE, visitor_id=vid1, event_type="ZONE_DWELL",        zone_id="SKINCARE", dwell_ms=45000, ts_offset_s=90, event_id="m-e3"),
        make_event(store_id=METRICS_STORE, visitor_id=vid1, event_type="BILLING_QUEUE_JOIN",zone_id="BILLING",  ts_offset_s=200, event_id="m-e4"),
        make_event(store_id=METRICS_STORE, visitor_id=vid1, event_type="EXIT",              ts_offset_s=300, event_id="m-e5"),
 
        # Visitor 2: enters, visits MAKEUP, abandons billing
        make_event(store_id=METRICS_STORE, visitor_id=vid2, event_type="ENTRY",               ts_offset_s=10,  event_id="m-e6"),
        make_event(store_id=METRICS_STORE, visitor_id=vid2, event_type="ZONE_ENTER",          zone_id="MAKEUP", ts_offset_s=70, event_id="m-e7"),
        make_event(store_id=METRICS_STORE, visitor_id=vid2, event_type="BILLING_QUEUE_JOIN",  zone_id="BILLING", ts_offset_s=210, event_id="m-e8"),
        make_event(store_id=METRICS_STORE, visitor_id=vid2, event_type="BILLING_QUEUE_ABANDON",zone_id="BILLING", ts_offset_s=250, event_id="m-e9"),
        make_event(store_id=METRICS_STORE, visitor_id=vid2, event_type="EXIT",                ts_offset_s=260, event_id="m-e10"),
 
        # Visitor 3: staff — must NOT appear in customer metrics
        make_event(store_id=METRICS_STORE, visitor_id=vid3, event_type="ZONE_ENTER", is_staff=True,
                   zone_id="SKINCARE", ts_offset_s=5, event_id="m-e11"),
    ]
    await client.post("/events/ingest", json={"events": events})
    yield
 
 
class TestMetrics:
    async def test_metrics_response_shape(self, client: AsyncClient, seed_metrics):
        resp = await client.get(f"/stores/{METRICS_STORE}/metrics")
        assert resp.status_code == 200
        body = resp.json()
        for key in ("store_id","unique_visitors","conversion_rate","avg_dwell_ms",
                    "zone_dwells","queue_depth","abandonment_rate","data_confidence"):
            assert key in body, f"Missing key: {key}"
 
    async def test_unique_visitors_excludes_staff(self, client: AsyncClient, seed_metrics):
        resp = await client.get(f"/stores/{METRICS_STORE}/metrics")
        body = resp.json()
        # 2 customer visitors (vid1, vid2); vid3 is staff
        assert body["unique_visitors"] == 2
 
    async def test_abandonment_rate_computed(self, client: AsyncClient, seed_metrics):
        resp = await client.get(f"/stores/{METRICS_STORE}/metrics")
        body = resp.json()
        # 1 abandon out of 2 billing_queue_join → 0.5
        assert body["abandonment_rate"] == pytest.approx(0.5, abs=0.01)
 
    async def test_empty_store_returns_zeros_not_null(self, client: AsyncClient):
        resp = await client.get("/stores/ST_EMPTY_STORE/metrics")
        assert resp.status_code == 200
        body = resp.json()
        assert body["unique_visitors"] == 0
        assert body["conversion_rate"] == 0.0
        assert body["queue_depth"] == 0
        assert body["zone_dwells"] == []
 
    async def test_zone_dwell_present(self, client: AsyncClient, seed_metrics):
        resp = await client.get(f"/stores/{METRICS_STORE}/metrics")
        body = resp.json()
        zones = {z["zone_id"] for z in body["zone_dwells"]}
        assert "SKINCARE" in zones
 
 
# ── GET /stores/{id}/funnel ───────────────────────────────────────────────────
 
FUNNEL_STORE = "ST_FUNNEL"
 
 
@pytest_asyncio.fixture(autouse=False)
async def seed_funnel(client: AsyncClient):
    """5 visitors at entry, 4 visit a zone, 2 reach billing, 1 purchases."""
    events = []
    for i in range(5):
        vid = f"VIS_F{i:03d}"
        events.append(make_event(store_id=FUNNEL_STORE, visitor_id=vid,
                                 event_type="ENTRY", ts_offset_s=i, event_id=f"f-entry-{i}"))
 
    for i in range(4):
        vid = f"VIS_F{i:03d}"
        events.append(make_event(store_id=FUNNEL_STORE, visitor_id=vid,
                                 event_type="ZONE_ENTER", zone_id="SKINCARE",
                                 ts_offset_s=60 + i, event_id=f"f-zone-{i}"))
 
    for i in range(2):
        vid = f"VIS_F{i:03d}"
        events.append(make_event(store_id=FUNNEL_STORE, visitor_id=vid,
                                 event_type="BILLING_QUEUE_JOIN", zone_id="BILLING",
                                 ts_offset_s=200 + i, event_id=f"f-bill-{i}"))
 
    # Visitor 1 abandons → only visitor 0 "purchased"
    events.append(make_event(store_id=FUNNEL_STORE, visitor_id="VIS_F001",
                             event_type="BILLING_QUEUE_ABANDON", zone_id="BILLING",
                             ts_offset_s=240, event_id="f-abandon-0"))
 
    await client.post("/events/ingest", json={"events": events})
    yield
 
 
class TestFunnel:
    async def test_funnel_stage_counts(self, client: AsyncClient, seed_funnel):
        resp = await client.get(f"/stores/{FUNNEL_STORE}/funnel")
        assert resp.status_code == 200
        body = resp.json()
        stages = {s["stage"]: s["count"] for s in body["stages"]}
        assert stages["ENTRY"]         == 5
        assert stages["ZONE_VISIT"]    == 4
        assert stages["BILLING_QUEUE"] == 2
        assert stages["PURCHASE"]      == 1
 
    async def test_funnel_drop_off_pct_calculated(self, client: AsyncClient, seed_funnel):
        resp = await client.get(f"/stores/{FUNNEL_STORE}/funnel")
        stages = {s["stage"]: s for s in resp.json()["stages"]}
        # ENTRY → ZONE_VISIT: 1 drop from 5 = 20%
        assert stages["ZONE_VISIT"]["drop_off_pct"] == pytest.approx(20.0, abs=0.5)
        # First stage always 0
        assert stages["ENTRY"]["drop_off_pct"] == 0.0
 
    async def test_reentry_not_double_counted(self, client: AsyncClient):
        """Same visitor_id appearing as ENTRY + REENTRY counts as 1 session."""
        vid = "VIS_REENTRY_01"
        events = [
            make_event(store_id="ST_REENTRY", visitor_id=vid, event_type="ENTRY",
                       ts_offset_s=0, event_id="re-entry-1"),
            make_event(store_id="ST_REENTRY", visitor_id=vid, event_type="EXIT",
                       ts_offset_s=100, event_id="re-exit-1"),
            make_event(store_id="ST_REENTRY", visitor_id=vid, event_type="REENTRY",
                       ts_offset_s=200, event_id="re-reentry-1"),
        ]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get("/stores/ST_REENTRY/funnel")
        stages = {s["stage"]: s["count"] for s in resp.json()["stages"]}
        # Still 1 unique visitor, not 2
        assert stages["ENTRY"] == 1
 
    async def test_empty_store_funnel_all_zeros(self, client: AsyncClient):
        resp = await client.get("/stores/ST_FUNNEL_EMPTY/funnel")
        assert resp.status_code == 200
        for stage in resp.json()["stages"]:
            assert stage["count"] == 0
 
 
# ── GET /stores/{id}/heatmap ──────────────────────────────────────────────────
 
class TestHeatmap:
    async def test_heatmap_normalised_max_is_100(self, client: AsyncClient, seed_metrics):
        resp = await client.get(f"/stores/{METRICS_STORE}/heatmap")
        assert resp.status_code == 200
        zones = resp.json()["zones"]
        if zones:
            normalised = [z["normalised"] for z in zones]
            assert max(normalised) == pytest.approx(100.0, abs=0.1)
 
    async def test_heatmap_empty_store(self, client: AsyncClient):
        resp = await client.get("/stores/ST_HEATMAP_EMPTY/heatmap")
        assert resp.status_code == 200
        body = resp.json()
        assert body["zones"] == []
        assert body["data_confidence"] is False
 
    async def test_heatmap_response_shape(self, client: AsyncClient, seed_metrics):
        resp = await client.get(f"/stores/{METRICS_STORE}/heatmap")
        body = resp.json()
        for key in ("store_id", "window", "zones", "data_confidence"):
            assert key in body
        for z in body["zones"]:
            assert "zone_id" in z
            assert "visit_count" in z
            assert "avg_dwell_ms" in z
            assert "normalised" in z
            assert 0.0 <= z["normalised"] <= 100.0
 
 
# ── GET /stores/{id}/anomalies ────────────────────────────────────────────────
 
class TestAnomalies:
    async def test_anomalies_response_shape(self, client: AsyncClient):
        resp = await client.get("/stores/ST_ANON_EMPTY/anomalies")
        assert resp.status_code == 200
        body = resp.json()
        assert "store_id" in body
        assert "anomalies" in body
        assert isinstance(body["anomalies"], list)
 
    async def test_no_anomalies_for_empty_store(self, client: AsyncClient):
        resp = await client.get("/stores/ST_ANON_EMPTY_2/anomalies")
        body = resp.json()
        # Empty store → no stale feed (never received events) → no anomalies
        non_stale = [a for a in body["anomalies"] if a["anomaly_type"] != "STALE_FEED"]
        assert non_stale == []
 
    async def test_queue_spike_anomaly_fires(self, client: AsyncClient):
        """Seed 6 BILLING_QUEUE_JOIN events (threshold=5) — expect BILLING_QUEUE_SPIKE."""
        from datetime import datetime, timezone
        now = datetime.now(tz=timezone.utc)
        events = [
            make_event(
                store_id="ST_QUEUE_SPIKE",
                visitor_id=f"VIS_Q{i}",
                event_type="BILLING_QUEUE_JOIN",
                zone_id="BILLING",
                # timestamp within last 15 min so it counts as live queue
                ts_offset_s=-(60 * 5) + i,   # 5 min ago
                event_id=f"qs-{i}",
            )
            for i in range(6)
        ]
        # Override timestamps to be recent (within last 15 min)
        recent_ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        for e in events:
            e["timestamp"] = recent_ts
 
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get("/stores/ST_QUEUE_SPIKE/anomalies")
        anomaly_types = [a["anomaly_type"] for a in resp.json()["anomalies"]]
        assert "BILLING_QUEUE_SPIKE" in anomaly_types
 
    async def test_anomaly_has_severity_and_action(self, client: AsyncClient):
        resp = await client.get("/stores/ST_QUEUE_SPIKE/anomalies")
        for anomaly in resp.json()["anomalies"]:
            assert anomaly["severity"] in ("INFO", "WARN", "CRITICAL")
            assert len(anomaly["suggested_action"]) > 0
            assert len(anomaly["description"]) > 0
 
 
# ── GET /health ───────────────────────────────────────────────────────────────
 
class TestHealth:
    async def test_health_returns_200(self, client: AsyncClient):
        resp = await client.get("/health")
        assert resp.status_code == 200
 
    async def test_health_db_connected(self, client: AsyncClient):
        resp = await client.get("/health")
        body = resp.json()
        assert body["db_connected"] is True
 
    async def test_health_response_shape(self, client: AsyncClient):
        resp = await client.get("/health")
        body = resp.json()
        for key in ("status", "db_connected", "stores", "checked_at"):
            assert key in body
        assert body["status"] in ("healthy", "degraded")
 
    async def test_health_store_entry_shape(self, client: AsyncClient, seed_metrics):
        resp = await client.get("/health")
        stores = {s["store_id"]: s for s in resp.json()["stores"]}
        assert METRICS_STORE in stores
        entry = stores[METRICS_STORE]
        assert "last_event_ts" in entry
        assert "lag_seconds" in entry
        assert entry["status"] in ("OK", "STALE_FEED", "NO_DATA")
 