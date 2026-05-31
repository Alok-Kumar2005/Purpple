import argparse
import json
import cv2
from pathlib import Path
from datetime import datetime, timezone
 
from tracker import PersonTracker
from emit import EventEmitter
from zones import ZoneMapper
 
 
def parse_args():
    p = argparse.ArgumentParser(description="Retail CCTV Detection Pipeline")
    p.add_argument("--video", required=True, help="Path to input video clip")
    p.add_argument("--store-id", required=True, help="Store ID from store_layout.json")
    p.add_argument("--camera-id", required=True,
                   help="Camera ID (CAM_ENTRY_01 | CAM_FLOOR_01 | CAM_BILLING_01)")
    p.add_argument("--layout", required=True, help="Path to store_layout.json")
    p.add_argument("--out", required=True, help="Output .jsonl file path")
    p.add_argument("--clip-start", required=True,
                   help="ISO-8601 UTC timestamp for frame-0 of the clip")
    p.add_argument("--conf", type=float, default=0.35,
                   help="YOLO detection confidence threshold")
    p.add_argument("--device", default="cpu",
                   help="Inference device: cpu | cuda | mps")
    p.add_argument("--skip-frames", type=int, default=2,
                   help="Process every Nth frame (2 = every other frame at 15fps → ~7.5fps)")
    return p.parse_args()
 
 
def load_layout(layout_path: str, store_id: str) -> dict:
    with open(layout_path) as f:
        layout = json.load(f)
    # Support both list-of-stores and direct single-store formats
    if isinstance(layout, list):
        for store in layout:
            if store.get("store_id") == store_id:
                return store
        raise ValueError(f"Store {store_id} not found in layout file")
    return layout
 
 
def main():
    args = parse_args()
 
    clip_start_dt = datetime.fromisoformat(
        args.clip_start.replace("Z", "+00:00")
    )
 
    layout = load_layout(args.layout, args.store_id)
 
    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {args.video}")
 
    fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f"[detect] Video: {args.video} | FPS: {fps:.1f} | Frames: {total_frames}")
 
    zone_mapper = ZoneMapper(layout, args.camera_id)
    tracker = PersonTracker(
        conf_thres=args.conf,
        device=args.device,
        camera_id=args.camera_id,
        fps=fps,
    )
    emitter = EventEmitter(
        store_id=args.store_id,
        camera_id=args.camera_id,
        clip_start=clip_start_dt,
        fps=fps,
        zone_mapper=zone_mapper,
        out_path=args.out,
    )
 
    frame_idx = 0
    processed = 0
 
    while True:
        ret, frame = cap.read()
        if not ret:
            break
 
        if frame_idx % args.skip_frames == 0:
            detections = tracker.update(frame, frame_idx)
            emitter.process(detections, frame_idx)
            processed += 1
 
            if processed % 100 == 0:
                pct = 100.0 * frame_idx / max(total_frames, 1)
                print(f"[detect] Frame {frame_idx}/{total_frames} ({pct:.1f}%) | "
                      f"Active tracks: {len(detections)}")
 
        frame_idx += 1
 
    # Flush any open sessions (no EXIT seen before clip ends)
    emitter.flush_open_sessions(frame_idx)
    cap.release()
 
    print(f"[detect] Done. Events written to: {args.out}")
    print(f"[detect] Summary: {emitter.summary()}")
 
 
if __name__ == "__main__":
    main()
 