"""
dashboard/replay.py — Simulated real-time event streamer.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import urllib.request
import urllib.error

BATCH_SIZE    = 50     # flush to API after this many events
BATCH_FLUSH_S = 2.0    # or after this many wall-clock seconds, whichever comes first


def parse_args():
    p = argparse.ArgumentParser(description="Real-time event replay into Store Intelligence API")
    p.add_argument("--file",  required=True,            help="events.jsonl from pipeline")
    p.add_argument("--url",   default="http://localhost:8000", help="API base URL")
    p.add_argument("--speed", type=float, default=10.0, help="Replay speed factor (10 = 10× real time)")
    p.add_argument("--store", default=None,             help="Filter to one store_id")
    p.add_argument("--loop",  action="store_true",      help="Loop the file indefinitely (demo mode)")
    return p.parse_args()


def post_batch(url: str, events: list[dict]) -> dict:
    payload = json.dumps({"events": events}).encode()
    req = urllib.request.Request(
        f"{url}/events/ingest",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def load_events(path: str, store_filter: Optional[str]) -> list[dict]:
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                if store_filter and e.get("store_id") != store_filter:
                    continue
                events.append(e)
            except json.JSONDecodeError:
                continue
    # Ensure sorted by timestamp
    events.sort(key=lambda e: e.get("timestamp", ""))
    return events


def parse_ts(ts_str: str) -> float:
    """Return UNIX timestamp float from ISO-8601 string."""
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()


def run_replay(events: list[dict], url: str, speed: float) -> None:
    if not events:
        print("[replay] No events to replay.")
        return

    t0_event  = parse_ts(events[0]["timestamp"])
    t0_wall   = time.time()

    batch: list[dict] = []
    batch_start_wall  = t0_wall

    total_sent = total_accepted = total_dup = 0

    for i, event in enumerate(events):
        # ── Timing: sleep until this event should fire ────────────────────────
        event_offset_s   = (parse_ts(event["timestamp"]) - t0_event) / speed
        target_wall      = t0_wall + event_offset_s
        sleep_for        = target_wall - time.time()
        if sleep_for > 0:
            time.sleep(sleep_for)

        # DYNAMIC TIME SHIFT FIX: Clone the historical event log entry and overwrite 
        # its timestamp property with the actual current wall-clock UTC string.
        # This forces historical events to land inside your live metrics window!
        event_copy = dict(event)
        event_copy["timestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        batch.append(event_copy)

        wall_now  = time.time()
        batch_age = wall_now - batch_start_wall
        is_last   = (i == len(events) - 1)

        # ── Flush condition ───────────────────────────────────────────────────
        if len(batch) >= BATCH_SIZE or batch_age >= BATCH_FLUSH_S or is_last:
            try:
                result = post_batch(url, batch)
                accepted = result.get("accepted", 0)
                dup      = result.get("duplicates", 0)
                total_sent     += len(batch)
                total_accepted += accepted
                total_dup      += dup

                pct = 100.0 * (i + 1) / len(events)
                clip_time = event_copy["timestamp"][11:19]  # Show live stream time
                print(
                    f"\r[replay] {pct:5.1f}% | stream_utc={clip_time} | "
                    f"sent={total_sent} accepted={total_accepted} dup={total_dup}",
                    end="",
                    flush=True,
                )
            except urllib.error.URLError as exc:
                print(f"\n[replay] WARN: POST failed: {exc} — retrying next batch")
            except Exception as exc:
                print(f"\n[replay] ERROR: {exc}")
                sys.exit(1)

            batch = []
            batch_start_wall = time.time()

    print(f"\n[replay] Complete. sent={total_sent} accepted={total_accepted} dup={total_dup}")


def main():
    args = parse_args()
    print(f"[replay] Loading {args.file} ...")
    events = load_events(args.file, args.store)
    print(f"[replay] {len(events)} events | speed={args.speed}× | API={args.url}")

    if not events:
        print("[replay] Nothing to send (check --store filter or file path).")
        sys.exit(0)

    run_num = 0
    while True:
        run_num += 1
        if args.loop:
            print(f"[replay] ─── Run #{run_num} ───")
        run_replay(events, args.url, args.speed)
        if not args.loop:
            break
        print("[replay] Looping in 3s ...")
        time.sleep(3)


if __name__ == "__main__":
    main()