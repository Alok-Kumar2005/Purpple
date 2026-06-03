# DESIGN.md — Store Intelligence System

## Architecture Overview

```
Raw CCTV Clips
     │
     ▼
┌─────────────────────────────────────────┐
│  Detection Pipeline  (pipeline/)        │
│                                         │
│  detect.py  ──► tracker.py             │
│      │           YOLOv8m + ByteTrack   │
│      │           Staff classification  │
│      ▼                                  │
│  zones.py   ──► emit.py                │
│  Polygon        Stateful event          │
│  zone map       machine                 │
│      │                                  │
│      ▼                                  │
│  events.jsonl  (one event per line)     │
└─────────────────────────────────────────┘
     │
     ▼
scripts/ingest_events.py  (batched POST, 500/call)
     │
     ▼
┌─────────────────────────────────────────┐
│  Intelligence API  (app/)               │
│                                         │
│  FastAPI + asyncpg + PostgreSQL         │
│                                         │
│  POST /events/ingest    ─► ingestion.py │
│  GET  /stores/{id}/metrics ─► metrics  │
│  GET  /stores/{id}/funnel  ─► funnel   │
│  GET  /stores/{id}/heatmap ─► heatmap  │
│  GET  /stores/{id}/anomalies ─► anomaly│
│  GET  /health           ─► health.py   │
└─────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────┐
│  Live Dashboard  (dashboard/)           │
│                                         │
│  Streamlit  polls API every 5s          │
│  Plotly funnel + heatmap + KPI cards    │
│                                         │
│  dashboard/replay.py                    │
│  Replays events.jsonl at N× speed       │
│  → makes dashboard update live          │
└─────────────────────────────────────────┘
```

---

## Stage 1 — Detection Pipeline

**Model**: YOLOv8m (medium). Detects `class=0` (person) only. Runs at ~7.5 fps
(every other frame of 15 fps source) to balance accuracy with processing speed.

**Tracking**: ByteTrack via the `supervision` library wrapper. Configured with a
3-second lost-track buffer, meaning a person who briefly disappears behind a display
will retain their track ID when they reappear.

**Staff detection** — two-signal heuristic, no custom training:
1. HSV-V channel mean of the torso ROI (middle third of bounding box) below
   threshold → dark/black clothing score
2. Track present in >40% of all processed frames → staff are always there

Both signals must be true before the `is_staff` label is locked, after 45 frames
(~6 seconds). A customer in a black dress fails signal 2.

**Zone mapping**: Polygon ray-casting against pixel-coordinate polygons defined
in `store_layout.json`. Fallback: frame-thirds heuristic if no polygons configured.
The foot-point (bottom-centre of bounding box) is used rather than bbox centroid
because people lean over shelves, making the centroid unreliable.

**Entry/exit direction**: The crossing-line method tracks Y-position of the
foot-point across 10+ frames. A crossing from above-line to below-line is ENTRY;
below to above is EXIT. The line position defaults to the vertical midpoint of the
entry camera frame and is configurable in `store_layout.json`.

**Re-ID**: Intra-clip re-ID is handled natively by ByteTrack's track buffer.
Inter-clip / long-absence re-ID is handled by matching new tracks against recently
closed sessions (within 5-minute window) in `emit.py`. The same `visitor_id` is
reused, producing a `REENTRY` event instead of a second `ENTRY`.

---

## Stage 2 — Event Schema

Events are emitted as newline-delimited JSON (`events.jsonl`). Every event carries:

- `event_id` — UUIDv4, the idempotency key for the API
- `visitor_id` — stable per-visit token; shared across ENTRY + REENTRY
- `is_staff` — set by the tracker; events with `is_staff=true` are ingested but
  excluded at query time (not filtered before ingest, to preserve audit trail)
- `confidence` — raw model confidence; never suppressed, so low-confidence
  detections appear with their actual score

---

## Stage 3 — Intelligence API

**Database**: PostgreSQL 16 with async SQLAlchemy + asyncpg driver.
Two tables:

- `events` — one row per event, indexed on `(store_id, ts)`, `(store_id, visitor_id)`,
  `(store_id, event_type)`. `event_id` has a unique constraint for idempotency.
- `daily_metric_cache` — per-store per-day aggregates, refreshed on every ingest
  batch. Used only by the anomaly detection 7-day history query, not by real-time
  metrics (which query `events` directly).

**Idempotency**: `INSERT ... ON CONFLICT (event_id) DO NOTHING RETURNING event_id`.
The `RETURNING` clause tells us exactly which rows were inserted vs skipped, so the
response accurately reports `accepted` / `duplicates` in a single query.

**Session deduplication in funnel**: A single SQL CTE groups all events by `visitor_id`,
using `BOOL_OR` aggregates to flag which stages each visitor reached. One visitor =
one row regardless of how many REENTRY events they have.

**Anomaly detection**:
- `BILLING_QUEUE_SPIKE` — counts live queue depth (JOIN minus ABANDON in last 15 min)
- `CONVERSION_DROP` — compares today's live rate against 7-day avg ± 2 std dev
- `DEAD_ZONE` — zones with visits earlier today but none in last 30 min
- `STALE_FEED` — `MAX(ts)` per store older than 10 minutes

---

## Stage 4 — Live Dashboard

Streamlit polls the API every 5 seconds (configurable). No WebSocket or SSE.
The `dashboard/replay.py` script replays `events.jsonl` at configurable speed
(default 10×) by sleeping proportionally between events and POSTing batches.

---

## AI-Assisted Decisions

### 1. Staff Detection Without Custom Training

**Problem**: Staff wear full-black uniforms; distinguishing them from customers in
black clothing required either a labelled dataset (unavailable) or a heuristic.

**What I asked the AI**: "Given retail CCTV footage where staff wear full black uniforms
and are present in every camera frame for the entire shift, design a staff detection
method that doesn't require model fine-tuning."

**What the AI suggested**: Use a VLM (GPT-4V or similar) on sampled frames to describe
clothing and classify staff. It also suggested a simpler HSV-threshold approach.

**What I chose**: The two-signal HSV + presence-ratio heuristic. The VLM approach
was rejected because: (a) it requires an API call per sampled frame — expensive and
slow for a 20-minute clip, (b) faces are blurred so the VLM loses important context,
(c) the presence-ratio signal alone is powerful (staff literally never leave) and
the AI underweighted it.

**Override verdict**: Disagreed with VLM suggestion. HSV + presence ratio is faster,
interpretable, and tuneable without retraining.

---

### 2. Idempotency via RETURNING Rather Than SELECT-then-INSERT

**Problem**: The ingest endpoint must distinguish accepted vs duplicate events to
return accurate counts without two database round-trips per event.

**What the AI suggested**: SELECT COUNT(*) to check existence, then INSERT if not found.
Standard approach but requires two queries and is subject to a race condition.

**What I chose**: Single `INSERT ... ON CONFLICT DO NOTHING RETURNING event_id`.
Rows returned = accepted; rows attempted minus returned = duplicates. One query, no
race condition, atomically correct.

**Override verdict**: Disagreed with AI's SELECT-then-INSERT. The RETURNING approach
is both faster and correct under concurrent ingestion.

---

### 3. Streaming: Batch-Flush Replay vs SSE vs WebSocket

**Problem**: The dashboard must update in real time as events come in from the
detection pipeline.

**What the AI suggested**: Server-Sent Events (SSE) endpoint on the API, with the
Streamlit dashboard using `requests` to consume it. The AI correctly identified that
WebSockets are overkill for one-way server→client flow.

**What I chose**: The simpler batch-flush replay loop (`dashboard/replay.py`). The
API already exposes polling endpoints. SSE would require a separate pub/sub
channel (Redis or Postgres `LISTEN/NOTIFY`) to broadcast events from the ingest
path to the SSE endpoint — extra infrastructure with no scoring benefit. The
Streamlit dashboard auto-refreshes every 5 seconds, which is well within human
perception of "live" for a retail metrics use case.

**Override verdict**: Agreed with SSE > WebSocket, but chose batch-flush polling
over SSE. The business value of a 1-second update vs a 5-second update is zero for
a store manager reading a dashboard.