# Store Intelligence — Apex Retail

End-to-end retail analytics: raw CCTV clips → live store metrics dashboard.

---

## Quick Start (5 commands)

```bash
git clone https://github.com/Alok-Kumar2005/Purpple
cd store-intelligence
docker compose up --build -d
# API:       http://localhost:8000
# Dashboard: http://localhost:8501
# Postgres:  localhost:5432
```

Then run the detection pipeline and feed results into the API:

```bash
./pipeline/run.sh --store-id ST1008 --clips-dir /data/clips/ \
                  --layout store_layout.json --out output/events.jsonl
python scripts/ingest_events.py --file output/events.jsonl
```

Watch the dashboard update live — or replay at speed:

```bash
python dashboard/replay.py --file output/events.jsonl --speed 10 --loop
```

---

## Architecture

```
CCTV Clips → pipeline/ → events.jsonl → API (FastAPI + Postgres) → Streamlit Dashboard
```

| Component | Location | Port |
|---|---|---|
| Intelligence API | `app/` | 8000 |
| Streamlit Dashboard | `dashboard/app.py` | 8501 |
| PostgreSQL | Docker service | 5432 |
| Detection Pipeline | `pipeline/` | — |

---

## Detection Pipeline

```bash
# Single clip
python pipeline/detect.py \
  --video /data/clips/entry.mp4 --store-id ST1008 \
  --camera-id CAM_ENTRY_01 --layout store_layout.json \
  --out events_entry.jsonl --clip-start 2026-04-10T20:00:00Z

# All clips for a store
./pipeline/run.sh --store-id ST1008 --clips-dir /data/clips/ \
  --layout store_layout.json --out output/events.jsonl \
  --device cpu   # or cuda
```

**Camera ID convention**: filenames containing `entry`/`cam1` → `CAM_ENTRY_01`,
`floor`/`cam2` → `CAM_FLOOR_01`, `billing`/`cam3` → `CAM_BILLING_01`.

**Staff detection** (no custom training): staff wear full black; detected by
HSV-V torso score + presence ratio >40% of frames. Tune in `pipeline/tracker.py`.

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/events/ingest` | Batch ingest (≤500 events). Idempotent by `event_id`. |
| `GET` | `/stores/{id}/metrics` | Live KPIs: visitors, conversion, dwell, queue, abandonment |
| `GET` | `/stores/{id}/funnel` | 4-stage funnel with drop-off % |
| `GET` | `/stores/{id}/heatmap` | Zone activity 0–100 normalised |
| `GET` | `/stores/{id}/anomalies` | Active anomalies with severity + action |
| `GET` | `/health` | DB status + per-store feed lag |

All `GET` endpoints accept `?window_hours=N` (1–168, default 24).

---

## Live Dashboard

```bash
# Option A: Docker (included in docker compose up)
open http://localhost:8501

# Option B: local dev
pip install streamlit plotly pandas requests
streamlit run dashboard/app.py
```

The dashboard polls the API every 5 seconds (adjustable via sidebar slider).
To drive live updates, run the replay script in parallel:

```bash
python dashboard/replay.py --file output/events.jsonl --speed 10 --loop
```

`--loop` replays the file continuously for demo mode.

---

## Tests

```bash
uv sync --extra dev
pytest tests/ -v

# Coverage
pytest tests/ --cov=app --cov-report=term-missing
```

Test files and what they cover:

| File | Covers |
|---|---|
| `tests/test_pipeline.py` | ZoneMapper, staff detection, EventEmitter edge cases |
| `tests/test_api.py` | All API endpoints, idempotency, funnel dedup |
| `tests/test_models.py` | Pydantic validation, all event types, boundary values |
| `tests/test_production.py` | 503 on DB failure, zero-purchase, all-staff, re-entry dedup |

---

## Configuration

Environment variables (set in `docker-compose.yml` or `.env`):

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://retail:retail@db:5432/store_intelligence` | Postgres DSN |
| `QUEUE_SPIKE_DEPTH` | `5` | Queue depth that triggers BILLING_QUEUE_SPIKE anomaly |
| `CONVERSION_DROP_STDDEV` | `2.0` | Std deviations below 7-day avg for CONVERSION_DROP |
| `DEAD_ZONE_MINUTES` | `30` | Minutes of inactivity before DEAD_ZONE fires |
| `STALE_FEED_MINUTES` | `10` | Feed lag before STALE_FEED warning |
| `API_URL` | `http://localhost:8000` | Dashboard → API URL |
| `STORE_ID` | `ST1008` | Default store shown in dashboard |
| `REFRESH_S` | `5` | Dashboard poll interval |

---

## Docs

- [`docs/DESIGN.md`](docs/DESIGN.md) — Architecture overview + AI-assisted decisions
- [`docs/CHOICES.md`](docs/CHOICES.md) — Three key technical decisions with full reasoning