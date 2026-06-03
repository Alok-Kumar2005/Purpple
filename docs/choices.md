# CHOICES.md — Three Key Technical Decisions

---

## Decision 1 — Detection Model: YOLOv8m

### Options Considered

| Model | Pros | Cons |
|---|---|---|
| YOLOv8n (nano) | Fastest, <10ms/frame | Lower accuracy on occlusion, misses small persons |
| **YOLOv8m (medium)** | **Good accuracy/speed balance, ~25ms/frame on CPU** | **Heavier than nano** |
| YOLOv8x (xlarge) | Best accuracy | ~100ms/frame on CPU — too slow for 15fps input |
| RT-DETR | Transformer-based, strong on crowd detection | Larger memory footprint, less mature tracking integration |
| MediaPipe Pose | Real-time, lightweight | Pose-first not detection-first; struggles with partial occlusion |

### What AI Suggested
When I described the footage (1080p, 15fps, partial occlusion, group entry, retail
lighting variation) and asked which YOLO variant to use, the AI recommended
**YOLOv8m** as the starting point — the same conclusion I reached independently.
It also flagged that YOLOv8x would be the right choice if a GPU was available and
latency wasn't a constraint.

### What I Chose and Why
**YOLOv8m**. Runs at ~7 fps on a modern CPU (processing every other frame of a 15fps
source), which is sufficient to track pedestrians whose position changes slowly
relative to frame rate. The medium model catches the partial-occlusion cases that
nano struggles with — particularly people partially behind the circular product
display visible in the footage. I skip every other frame (`--skip-frames 2`) to
maintain near-real-time throughput on CPU.

If this were a production deployment at 40 stores with dedicated GPU inference
nodes, I would switch to YOLOv8x or RT-DETR and process every frame.

### VLM Usage
I did not use a VLM for person detection — YOLO is purpose-built for this and
significantly faster. I evaluated using Claude Vision to classify staff vs customer
on sampled frames, but rejected it (see DESIGN.md, AI-Assisted Decision 1).

---

## Decision 2 — Event Schema Design

### Options Considered

**Option A**: Minimal schema — just `(visitor_id, event_type, timestamp, zone_id)`.
Easy to emit, harder to query. Every metric computation needs multiple self-joins.

**Option B**: Fat schema — embed full bounding-box coordinates, all track metadata,
raw confidence scores. Complete audit trail but heavy storage and noisy for the API.

**Option C (chosen)**: Behavioural event schema — captures the *meaning* of a detection
(ENTRY, ZONE_DWELL) rather than raw pixel data. Includes `confidence` and `is_staff`
for filtering, `metadata.session_seq` for ordering events within a visit, `dwell_ms`
pre-computed at emit time.

### What AI Suggested
The AI's first-pass schema had `visitor_id` as a simple incrementing integer rather
than a stable Re-ID token. This would break funnel deduplication — if ByteTrack
reassigns a track ID on re-entry, the visitor would appear twice. I changed
`visitor_id` to be a UUID-prefix string assigned once per physical person per visit,
shared across REENTRY events.

The AI also suggested omitting `confidence` to keep the schema clean. I kept it —
the problem statement explicitly requires "degrading gracefully" on partial occlusion,
and suppressing low-confidence events would hide the degradation from downstream
consumers.

### What I Chose and Why
Option C, with two specific overrides of the AI's suggestion:
1. `visitor_id` is a stable Re-ID token, not a raw track ID
2. `confidence` is always included, never suppressed

The schema is designed so every analytics query can be answered with a single GROUP BY
on `(store_id, visitor_id)` without requiring session reconstruction.

---

## Decision 3 — API Architecture: Sync Query vs Materialised Cache

### Options Considered

**Option A**: All metrics computed live from the raw `events` table on every request.
Always accurate, but at 40 stores × multiple concurrent dashboard users, the
`COUNT(DISTINCT visitor_id)` queries would scan millions of rows without good indexing.

**Option B**: Full materialisation — run a background job every minute, write all
metrics to a cache table, serve metrics from cache only.
Fast reads, but metrics are always up to N minutes stale. Unacceptable for the
queue-depth metric (should reflect current state).

**Option C (chosen)**: Hybrid — real-time metrics query live `events` on every
request, but anomaly detection uses a `daily_metric_cache` table (refreshed on each
ingest batch) for the 7-day history lookups.

### What AI Suggested
The AI suggested using Redis as an in-memory cache layer between the API and
Postgres, with a 30-second TTL. While this would handle concurrent dashboard users
elegantly, it introduces a third dependency (`redis` service, `aioredis` client,
cache invalidation logic). For an MVP evaluated on a single test store, the
complexity cost outweighs the performance benefit.

### What I Chose and Why
Option C. The compound index `(store_id, ts)` on the `events` table makes the
live queries fast enough for the evaluation load. The `daily_metric_cache` table is
populated as a side-effect of each ingest batch — no separate background job, no
new service.

If this were deployed to all 40 stores with live camera feeds, the right next step
would be: (a) add Postgres partitioning by `store_id + month`, (b) add Redis for
the queue-depth metric specifically (hot path, ~10 reads/second), (c) keep
everything else as live queries.

**Override verdict**: Disagreed with Redis suggestion for MVP. Agreed with the
AI's general point that the queue-depth metric is the hottest path and would be
the first to bottleneck at scale.